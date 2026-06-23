"""Sophee Discord bot client.

Main entry point — handles Discord events, message routing to ADK agents,
session management, and user preference injection.
"""

import asyncio
import base64
import logging
import os
import sys

import discord
from dotenv import load_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from app.agent import root_agent
from app.radio_state import set_discord_client
from bot.cache import save_image_metadata
from bot.history import trim_session_history
from bot.message_utils import (
    bracket_urls,
    fetch_chunked_context,
    read_image_attachment,
    send_message_in_chunks,
)
from bot.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sophee.bot.client")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
APP_NAME = "app"

# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ---------------------------------------------------------------------------
# ADK services
# ---------------------------------------------------------------------------

session_service = DatabaseSessionService(db_url="sqlite+aiosqlite:///sessions.db")
artifact_service = InMemoryArtifactService()

runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=session_service,
    artifact_service=artifact_service,
)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

rate_limiter = RateLimiter(cooldown_seconds=3.0)

# Prevent background tasks from being garbage collected
background_tasks = set()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

async def get_or_create_session(user_id: str, session_id: str):
    """Gets an existing session or creates a new one."""
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    return session


def update_session_state(user_id: str, session_id: str, updates: dict):
    """Synchronous helper to update session state directly.
    Used by views/modals that need to set state before running the agent.
    """
    async def _update():
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if session:
            session.state.update(updates)
            # Persist state changes via a dummy system event
            from google.adk.events import Event, EventActions
            import time
            import uuid
            dummy_event = Event(
                timestamp=time.time(),
                author="system",
                invocation_id=f"state_update_{uuid.uuid4().hex[:8]}",
                actions=EventActions(state_delta=updates),
            )
            await session_service.append_event(session, dummy_event)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_update())
    except RuntimeError:
        asyncio.run(_update())


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    set_discord_client(client)
    logger.info("Sophee is online as %s (ID: %s)", client.user, client.user.id)
    logger.info("Connected to %d guilds", len(client.guilds))

    # Run image cache janitor task
    from bot.cache import cleanup_image_metadata
    asyncio.create_task(cleanup_image_metadata())


@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages and DMs
    if message.author == client.user:
        return
    if not message.guild:
        return

    # Check if bot is mentioned or replied to
    is_mentioned = client.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and hasattr(message.reference.resolved, "author")
        and message.reference.resolved.author == client.user
    )

    if not is_mentioned and not is_reply_to_bot:
        return

    # Rate limiting
    user_id_str = str(message.author.id)
    if not rate_limiter.check(user_id_str):
        remaining = rate_limiter.remaining(user_id_str)
        await message.add_reaction("⏳")
        logger.debug("Rate limited user %s (%.1fs remaining)", user_id_str, remaining)
        return

    # Session IDs
    user_id = str(message.author.id)
    session_id = f"discord_{message.channel.id}"

    if message.guild:
        from app.radio_state import register_channel_guild
        register_channel_guild(message.channel.id, message.guild.id)

    session = await get_or_create_session(user_id, session_id)

    # Trim history before running agent
    await trim_session_history(session_service, APP_NAME, user_id, session_id)

    # Process message content
    msg_text = message.content.replace(f"<@{client.user.id}>", "").strip()

    # Handle reply context reconstruction (both bot and user messages)
    if message.reference and message.reference.resolved:
        replied_msg = message.reference.resolved
        chunked_context = await fetch_chunked_context(replied_msg)
        if chunked_context:
            msg_text = f"[The user is replying to this message from {replied_msg.author.display_name}:\n---\n{chunked_context}\n---\n]\n\n{msg_text}"

    # Process image attachments
    image_data = None
    if message.attachments:
        for attachment in message.attachments:
            result = await read_image_attachment(attachment)
            if result:
                image_data = result
                break

    if image_data:
        session.state["latest_input_image"] = {
            "data": image_data["data"],
            "mime_type": image_data["mime_type"],
        }

    # Build user preference context
    user_prefs = session.state.get("user_prefs", {})
    corrections = user_prefs.get("corrections", [])
    pref_context = ""
    if corrections:
        prefs_str = "\n".join(f"- {c}" for c in corrections)
        pref_context = f"\n\n[USER PREFERENCES for this user:\n{prefs_str}]"

    # Build the message parts
    parts = []
    if image_data:
        parts.append(types.Part.from_bytes(
            data=image_data["raw_bytes"],
            mime_type=image_data["mime_type"],
        ))

    full_text = msg_text + pref_context if pref_context else msg_text
    parts.append(types.Part.from_text(text=full_text))

    new_message = types.Content(role="user", parts=parts)

    # Track artifacts before the run
    before_keys = set(
        await artifact_service.list_artifact_keys(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    )

    # Show typing indicator while processing
    async with message.channel.typing():
        response_text = ""
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message,
            ):
                if event.is_final_response():
                    response_parts = (
                        event.content.parts
                        if (event.content and event.content.parts)
                        else []
                    )
                    response_text += "".join([p.text for p in response_parts if p.text])
        except Exception as e:
            logger.exception("Error running ADK agent:")
            await message.reply(f"An error occurred while processing your request: {e}")
            return

    # Refresh session to get updated state
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    # Check for new artifacts (images, TTS)
    after_keys = set(
        await artifact_service.list_artifact_keys(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    )
    new_keys = after_keys - before_keys

    # Handle new image artifacts
    new_image_key = None
    for key in new_keys:
        if key.endswith((".jpeg", ".jpg", ".png")):
            new_image_key = key
            break

    if new_image_key:
        import tempfile
        from bot.views import ImageView

        part_data = await artifact_service.load_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            filename=new_image_key,
            session_id=session_id,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg", mode="wb") as f:
            f.write(part_data.inline_data.data)
            temp_file_path = f.name

        view = ImageView(
            user_id, session_id,
            runner, artifact_service, session_service, update_session_state,
        )

        sent_msg = await message.reply(
            content=None,
            file=discord.File(temp_file_path),
            view=view,
        )
        os.remove(temp_file_path)

        # Save metadata for edit/reroll/restyle
        await save_image_metadata(
            message_id=str(sent_msg.id),
            prompt=msg_text,
            style=session.state.get("rolled_style") if session else None,
            resolution=session.state.get("latest_resolution", "0.5k") if session else "0.5k",
            session_id=session.state.get("last_image_interaction_id") if session else None,
        )

        # Send text response in an archived thread
        if response_text:
            try:
                active_thread = await sent_msg.create_thread(name="Image Details")
                await send_message_in_chunks(active_thread, response_text, is_thread=True)
                await active_thread.edit(archived=True)
            except Exception as thread_err:
                logger.warning("Error creating image thread: %s", thread_err)
                await send_message_in_chunks(message, response_text)
        return

    # Handle new TTS artifacts
    new_tts_key = None
    for key in new_keys:
        if key.endswith(".wav"):
            new_tts_key = key
            break

    if new_tts_key:
        import tempfile

        part_data = await artifact_service.load_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            filename=new_tts_key,
            session_id=session_id,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav", mode="wb") as f:
            f.write(part_data.inline_data.data)
            temp_file_path = f.name

        await message.reply(
            content=response_text if response_text else None,
            file=discord.File(temp_file_path),
        )
        os.remove(temp_file_path)
        return

    # Handle staged station (launch embed — only appears when starting a NEW station)
    staged_tracks = session.state.get("staged_station_tracks") if session else None
    if staged_tracks:
        from bot.views import RadioView, create_radio_embed

        playlist_data = {
            "tracks": staged_tracks,
            "playlist_thesis": session.state.get("staged_station_thesis", "music"),
            "mode": session.state.get("staged_station_mode", "standard"),
            "seed_tags": session.state.get("staged_station_seed_tags", []),
        }

        # Clear the staged data and persist it so it's only shown once
        session.state["staged_station_tracks"] = None
        session.state["staged_station_thesis"] = None
        session.state["staged_station_mode"] = None
        session.state["staged_station_seed_tags"] = None

        from google.adk.events import Event, EventActions
        import time
        import uuid
        dummy_event = Event(
            timestamp=time.time(),
            author="system",
            invocation_id=f"state_update_{uuid.uuid4().hex[:8]}",
            actions=EventActions(
                state_delta={
                    "staged_station_tracks": None,
                    "staged_station_thesis": None,
                    "staged_station_mode": None,
                    "staged_station_seed_tags": None,
                }
            ),
        )
        await session_service.append_event(session, dummy_event)

        embed = create_radio_embed(playlist_data)

        view = RadioView(
            playlist_data, user_id, session_id, session_service, update_session_state
        )

        if response_text:
            await send_message_in_chunks(message, response_text)
        await message.channel.send(embed=embed, view=view)
        return

    # Default: send text response
    if response_text:
        await send_message_in_chunks(message, response_text)
    else:
        await message.reply("I processed your request but have no text response.")


# ---------------------------------------------------------------------------
# Reset conversation command
# ---------------------------------------------------------------------------

@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Ignore message edits to prevent double-processing."""
    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in .env file!")
        sys.exit(1)

    logger.info("Starting Sophee Discord bot...")
    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
