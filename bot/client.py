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
from discord import app_commands
from dotenv import load_dotenv
from google.adk.artifacts import FileArtifactService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from app.db import session_service
from google.genai import types

from app.agent import root_agent
from app.radio_state import set_discord_client
from bot.cache import save_image_metadata, get_image_metadata
from bot.history import trim_session_history
from bot.message_utils import (
    bracket_urls,
    fetch_chunked_context,
    fetch_conversation_context,
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
try:
    DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
except ValueError:
    DISCORD_GUILD_ID = 0
APP_NAME = "app"

# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------------------------------------------------------------------------
# ADK services
# ---------------------------------------------------------------------------


project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
artifacts_dir = os.path.join(project_root, "data", "artifacts")
artifact_service = FileArtifactService(root_dir=artifacts_dir)

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


async def update_session_state(user_id: str, session_id: str, updates: dict):
    """Async helper to update session state directly and persist it.
    Used by views/modals that need to set state before running the agent.
    """
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    if session:
        session.state.update(updates)
        # In ADK, all state mutations outside the agent loop must be persisted via an Event.
        from google.adk.events import Event, EventActions
        import time
        import uuid
        state_event = Event(
            timestamp=time.time(),
            author="system",
            invocation_id=f"state_update_{uuid.uuid4().hex[:8]}",
            actions=EventActions(state_delta=updates),
        )
        await session_service.append_event(session, state_event)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    set_discord_client(client)
    logger.info("Sophee is online as %s (ID: %s)", client.user, client.user.id)
    logger.info("Connected to %d guilds", len(client.guilds))

    # Sync slash commands to the guild (instant, no global propagation delay)
    try:
        if DISCORD_GUILD_ID:
            guild_obj = discord.Object(id=DISCORD_GUILD_ID)
            synced = await tree.sync(guild=guild_obj)
            logger.info("Synced %d slash command(s) to guild %d", len(synced), DISCORD_GUILD_ID)
        else:
            logger.warning("DISCORD_GUILD_ID not set — slash commands not synced. Set it in .env.")
    except Exception as e:
        logger.error("Failed to sync slash commands: %s", e)

    # Run image cache janitor task
    from bot.cache import cleanup_image_metadata
    asyncio.create_task(cleanup_image_metadata())

    # Scan and resurrect active radio stations
    async def _scan_and_resurrect():
        try:
            # Wait a few seconds for the client cache to populate channels
            await asyncio.sleep(5)
            sessions_response = await session_service.list_sessions(app_name=APP_NAME)
            for session in sessions_response.sessions:
                active_radio = session.state.get("active_radio")
                if active_radio and active_radio.get("active"):
                    logger.info("Found active radio session to resurrect: %s", session.id)
                    asyncio.create_task(resurrect_radio_station(session, active_radio))
        except Exception as e:
            logger.error("Failed to resurrect active radio stations: %s", e)

    asyncio.create_task(_scan_and_resurrect())


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

def _guild_obj():
    """Returns the guild Object for slash command scoping, or None if not configured."""
    return discord.Object(id=DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None


@tree.command(name="image_settings", description="View and configure image generation defaults", guilds=[discord.Object(id=DISCORD_GUILD_ID)] if DISCORD_GUILD_ID else None)
async def cmd_image_settings(interaction: discord.Interaction):
    """Opens the image generation settings panel (ephemeral)."""
    user_id = str(interaction.user.id)
    session_id = f"discord_{interaction.channel_id}"
    session = await get_or_create_session(user_id, session_id)
    from bot.views import create_image_settings_view
    embed, view = create_image_settings_view(
        session.state if session else {},
        user_id,
        session_id,
        update_session_state,
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="llm_settings", description="Configure the general assistant's creativity and thinking level", guilds=[discord.Object(id=DISCORD_GUILD_ID)] if DISCORD_GUILD_ID else None)
async def cmd_llm_settings(interaction: discord.Interaction):
    """Opens the LLM settings panel (ephemeral). Only affects the general conversational agent."""
    user_id = str(interaction.user.id)
    session_id = f"discord_{interaction.channel_id}"
    session = await get_or_create_session(user_id, session_id)
    state = session.state if session else {}

    current_temp = state.get("llm_temperature", "model default")
    current_thinking = state.get("llm_thinking_level", "model default")

    embed = discord.Embed(
        title="🤖 General Assistant Settings",
        description="These settings only affect the **general conversational agent** (chit-chat, Q&A, writing, coding). Other agents like the DJ and Art Director are unaffected.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Current Settings",
        value=f"**Temperature:** `{current_temp}`\n**Thinking Level:** `{current_thinking}`",
        inline=False,
    )
    embed.set_footer(text="These settings persist for the duration of your session.")

    from bot.views import LLMSettingsView
    view = LLMSettingsView(state, update_session_state, user_id, session_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def resurrect_radio_station(session, active_radio):
    """Reconnects to the voice channel and restarts player tasks to resume radio playback after bot reboot."""
    from app.radio_state import set_radio_state, resolve_guild_id
    from bot.audio import audio_player_task, build_radio_sequence
    import discord

    text_channel_id = active_radio.get("text_channel_id")
    voice_channel_id = active_radio.get("voice_channel_id")

    if not text_channel_id or not voice_channel_id:
        logger.warning("Missing text/voice channel ID in active_radio state for session %s", session.id)
        return

    guild_id = resolve_guild_id(text_channel_id)
    if not guild_id:
        logger.warning("Could not resolve guild ID for text channel %s", text_channel_id)
        return

    # Wait for the discord client to be fully ready
    await client.wait_until_ready()

    voice_channel = client.get_channel(voice_channel_id)
    text_channel = client.get_channel(text_channel_id)
    if not voice_channel or not text_channel:
        logger.warning("Could not find voice channel %s or text channel %s to resurrect.", voice_channel_id, text_channel_id)
        return

    logger.info("Resurrecting radio station in voice channel: %s", voice_channel.name)
    try:
        vc = await voice_channel.connect()
    except Exception as e:
        logger.error("Failed to reconnect to voice channel %s during resurrection: %s", voice_channel_id, e)
        return

    # Restore the in-memory state
    active_radio["active"] = True
    set_radio_state(guild_id, active_radio)

    # Spawn player tasks
    abort_event = asyncio.Event()
    audio_queue = asyncio.Queue(maxsize=3)
    task1 = asyncio.create_task(
        audio_player_task(vc, audio_queue, text_channel, abort_event)
    )
    task2 = asyncio.create_task(
        build_radio_sequence(
            audio_queue, active_radio.get("use_dj", False), guild_id,
            session_service, None,
            text_channel_id, abort_event
        )
    )

    for task in (task1, task2):
        task.add_done_callback(lambda t: None)

    logger.info("Radio station resurrected successfully for guild %s", guild_id)
    try:
        await text_channel.send("📻 **System reboot detected.** Resuming your radio broadcast right where it left off!")
    except Exception as e:
        logger.warning("Failed to send resurrection message to text channel: %s", e)



async def execute_agent_turn(
    channel,
    author,
    content: str,
    user_id: str,
    session_id: str,
    message_reference=None,
    image_data=None,
    interaction=None,
):
    """Executes a single conversational agent turn, processes response artifacts (images, TTS),
    and displays RPG adventure stats/choices if an adventure is active.
    """
    session = await get_or_create_session(user_id, session_id)

    # Trim history before running agent
    await trim_session_history(session_service, APP_NAME, user_id, session_id)

    # We will accumulate any state changes required for this turn
    state_updates = {
        "latest_input_image": None,
        "latest_input_image_artifact": None
    }

    # Resolve image sequence threading via replies to image messages
    from bot.cache import get_image_metadata
    is_image_reply = False
    if message_reference and message_reference.reference and message_reference.reference.resolved:
        replied_msg = message_reference.reference.resolved
        ref_meta = await get_image_metadata(str(replied_msg.id))
        if ref_meta:
            is_image_reply = True
            artifact_name = ref_meta.get("image_artifact")
            if artifact_name:
                parent_artifact = ref_meta.get("parent_image_artifact")
                # If prompt refers to original/source/first/initial/seed, load parent artifact
                prompt_lower = content.lower()
                if parent_artifact and any(kw in prompt_lower for kw in ["original", "source", "first", "initial", "seed"]):
                    logger.info("User requested original composition. Routing reference image to parent artifact: %s", parent_artifact)
                    artifact_name = parent_artifact
                try:
                    part = await artifact_service.load_artifact(
                        app_name=APP_NAME,
                        user_id=user_id,
                        filename=artifact_name,
                        session_id=session_id,
                    )
                    if part and part.inline_data and part.inline_data.data:
                        import base64
                        img_b64 = base64.b64encode(part.inline_data.data).decode("utf-8")
                        state_updates["latest_input_image"] = {
                            "data": img_b64,
                            "mime_type": part.inline_data.mime_type or "image/jpeg",
                            "original_prompt": ref_meta.get("prompt") or "Generate an image",
                        }
                        state_updates["latest_input_image_artifact"] = artifact_name
                        logger.info("Loaded reply reference image from artifact: %s", artifact_name)
                except Exception as e:
                    logger.error("Failed to load reply reference image artifact %s: %s", artifact_name, e)


    # Process message content
    msg_text = content

    # Handle reply context reconstruction (both bot and user messages)
    if message_reference and message_reference.reference and message_reference.reference.resolved and not is_image_reply:
        replied_msg = message_reference.reference.resolved
        
        # Check if user wants the whole conversation starting from the reply
        prompt_lower = msg_text.lower()
        conv_keywords = ["this conversation", "this context", "the conversation", "these messages", "read above", "full context"]
        
        if any(kw in prompt_lower for kw in conv_keywords):
            # Fetch full conversation up to current message
            conv_context = await fetch_conversation_context(replied_msg, current_message=None)
            if conv_context:
                msg_text = f"[The user wants you to read the conversation starting from this message:\n---\n{conv_context}\n---\n]\n\n{msg_text}"
        else:
            # Default behavior: grab just the chunks of the single replied message
            chunked_context = await fetch_chunked_context(replied_msg)
            if chunked_context:
                msg_text = f"[The user is replying to this message from {replied_msg.author.display_name}:\n---\n{chunked_context}\n---\n]\n\n{msg_text}"

    # Inject adventure state if active
    is_adv_active = session and session.state.get("adventure_active")
    if is_adv_active:
        genre = session.state.get("genre", "Fantasy")
        char = session.state.get("character_concept", "Traveler")
        health = session.state.get("player_health", "100/100")
        inv = ", ".join(session.state.get("inventory", [])) or "None"
        quests = ", ".join(session.state.get("quest_log", [])) or "None"
        tension = session.state.get("tension", 10)

        state_info = (
            f"\n\n[SYSTEM INFO: Active Adventure Thread]\n"
            f"[Current Genre: {genre}]\n"
            f"[Character Concept: {char}]\n"
            f"[Player Health: {health}]\n"
            f"[Inventory: {inv}]\n"
            f"[Quest Log: {quests}]\n"
            f"[Current Tension Level: {tension}/100]"
        )
        msg_text = msg_text + state_info

    # Process image attachments
    if image_data:
        state_updates["latest_input_image"] = {
            "data": image_data["data"],
            "mime_type": image_data["mime_type"],
            "original_prompt": "Uploaded reference image",
        }
        # Save user uploaded image as an artifact so it can be referenced in edits
        import hashlib
        uploaded_key = f"user:uploaded_image_{hashlib.md5(image_data['data'].encode()).hexdigest()[:8]}.jpeg"
        part = types.Part(
            inline_data=types.Blob(mime_type=image_data["mime_type"], data=image_data["raw_bytes"])
        )
        await artifact_service.save_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            filename=uploaded_key,
            session_id=session_id,
            artifact=part
        )
        state_updates["latest_input_image_artifact"] = uploaded_key

    # Persist the state updates before the agent run
    if state_updates:
        await update_session_state(user_id, session_id, state_updates)

    # Build user preference context
    user_prefs = session.state.get("user_prefs", {})
    corrections = user_prefs.get("corrections", [])
    pref_context = ""
    
    # Try reading from file first
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(project_root, "data", f"user_profile_{user_id}.txt")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                pref_context = f.read()
            logger.debug("Loaded user personalization from file: %s", file_path)
        except Exception as e:
            logger.warning("Error reading user personalization file: %s", e)
            
    # Fallback to database session state and heal the file if missing
    if not pref_context and corrections:
        prefs_str = "\n".join(f"- {c}" for c in corrections)
        pref_context = f"\n\n[USER PREFERENCES for this user:\n{prefs_str}]"
        # Recreate the file to heal it
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(pref_context)
            logger.info("Healed/recreated user personalization file: %s", file_path)
        except Exception as e:
            logger.warning("Failed to recreate user personalization file: %s", e)

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

    response_text = ""
    async def _run_agent():
        nonlocal response_text
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
            raise e

    if interaction:
        await _run_agent()
    else:
        async with channel.typing():
            await _run_agent()

    # Refresh session to get updated state
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    embed = None
    view = None

    # Check for personalization profile embed flag
    show_profile = session.state.get("show_user_profile_embed") if session else None
    if show_profile:
        # Clear the flag so it doesn't trigger repeatedly
        await update_session_state(user_id, session_id, {"show_user_profile_embed": None})
        from bot.views import create_user_profile_embed
        embed = create_user_profile_embed(session.state.get("user_prefs", {}))

    # Check for image settings embed flag
    show_image_settings = session.state.get("show_image_settings_embed") if session else None
    if show_image_settings:
        await update_session_state(user_id, session_id, {"show_image_settings_embed": None})
        from bot.views import create_image_settings_view
        embed, view = create_image_settings_view(session.state, user_id, session_id, update_session_state)
        
    # Check for radio settings embed flag
    show_radio_settings = session.state.get("show_radio_settings_embed") if session else None
    if show_radio_settings:
        await update_session_state(user_id, session_id, {"show_radio_settings_embed": None})
        from bot.views import RadioSettingsView
        # We need the guild ID from the channel
        guild_id = channel.guild.id if hasattr(channel, "guild") else None
        if guild_id:
            from app.radio_state import active_radios
            if guild_id not in active_radios:
                active_radios[guild_id] = {"active": False, "mode": "standard", "jit_enabled": True}
            view = RadioSettingsView(guild_id)
            embed = discord.Embed(
                title="⚙️ Radio Settings", 
                description="Configure how your radio station plays tracks and curates music.",
                color=discord.Color.blurple()
            )

    # Check for new artifacts (images, TTS)
    after_keys = set(
        await artifact_service.list_artifact_keys(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    )
    new_keys = after_keys - before_keys
    logger.info("Artifact keys diff: before=%s, after=%s, new=%s", list(before_keys), list(after_keys), list(new_keys))

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

        if message_reference and hasattr(message_reference, "reply"):
            sent_msg = await message_reference.reply(
                content=None,
                file=discord.File(temp_file_path),
                view=view,
            )
        else:
            sent_msg = await channel.send(
                content=None,
                file=discord.File(temp_file_path),
                view=view,
            )
        os.remove(temp_file_path)

        # Determine the isolated session ID for this image thread
        image_session_id = session_id
        if session_id == f"discord_{channel.id}":
            image_session_id = f"discord_image_{sent_msg.id}"

        # Save metadata for edit/reroll/restyle
        last_prompt = session.state.get("last_generated_prompt") or content
        await save_image_metadata(
            message_id=str(sent_msg.id),
            prompt=last_prompt,
            style=session.state.get("rolled_style") if session else None,
            resolution=session.state.get("latest_resolution", "0.5k") if session else "0.5k",
            image_artifact=new_image_key,
            parent_image_artifact=session.state.get("latest_input_image_artifact") if session else None,
            session_id=image_session_id,
        )
        
        # Tie image to the ADK history by appending a Markdown link to the last bot message
        try:
            import sqlite3, json, os
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, event_data FROM events 
                WHERE session_id = ? AND user_id = ? 
                ORDER BY timestamp DESC LIMIT 20
            """, (session_id, user_id))
            for row in cursor.fetchall():
                event_id, event_data_str = row
                try:
                    event_data = json.loads(event_data_str)
                    if event_data.get("author") not in ("user", "system") and event_data.get("author"):
                        md_text = f"\n\n![image](/api/artifacts/{user_id}/{session_id}/{new_image_key})"
                        if "content" in event_data and "parts" in event_data["content"]:
                            parts = event_data["content"]["parts"]
                            if parts and "text" in parts[-1]:
                                parts[-1]["text"] += md_text
                            else:
                                parts.append({"text": md_text})
                            cursor.execute("UPDATE events SET event_data = ? WHERE id = ?", (json.dumps(event_data), event_id))
                            conn.commit()
                        break
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            logger.error("Failed to link artifact to history in client: %s", e)

        # Send text response in an archived thread
        if response_text:
            try:
                active_thread = await sent_msg.create_thread(name="Image Details")
                await send_message_in_chunks(active_thread, response_text, is_thread=True)
                await active_thread.edit(archived=True)
            except Exception as thread_err:
                logger.warning("Error creating image thread: %s", thread_err)
                if message_reference:
                    await send_message_in_chunks(message_reference, response_text)
                else:
                    await send_message_in_chunks(channel, response_text)
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

        if message_reference and hasattr(message_reference, "reply"):
            await message_reference.reply(
                content=response_text if response_text else None,
                file=discord.File(temp_file_path),
            )
        else:
            await channel.send(
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
            "candidate_pool_seeds": session.state.get("staged_station_candidate_pool_seeds", []),
        }

        # Clear the staged data and persist it so it's only shown once
        session.state["staged_station_tracks"] = None
        session.state["staged_station_thesis"] = None
        session.state["staged_station_mode"] = None
        session.state["staged_station_seed_tags"] = None
        session.state["staged_station_candidate_pool_seeds"] = None

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
                    "staged_station_candidate_pool_seeds": None,
                }
            ),
        )
        await session_service.append_event(session, dummy_event)

        embed = create_radio_embed(playlist_data)
        view = RadioView(
            playlist_data, user_id, session_id, session_service, update_session_state
        )

        if response_text:
            if message_reference:
                await send_message_in_chunks(message_reference, response_text)
            else:
                await send_message_in_chunks(channel, response_text)
        await channel.send(embed=embed, view=view)
        return

    # Handle staged station stop (boot bot from voice channel)
    if session and session.state.get("staged_station_stop"):
        session.state["staged_station_stop"] = False
        from google.adk.events import Event, EventActions
        import time
        import uuid
        dummy_event = Event(
            timestamp=time.time(),
            author="system",
            invocation_id=f"state_update_{uuid.uuid4().hex[:8]}",
            actions=EventActions(state_delta={"staged_station_stop": False}),
        )
        await session_service.append_event(session, dummy_event)

        if hasattr(channel, "guild") and channel.guild and channel.guild.voice_client:
            try:
                await channel.guild.voice_client.disconnect(force=True)
            except Exception as e:
                logger.warning("Failed to disconnect voice client on staged_station_stop: %s", e)

        if response_text:
            if message_reference:
                await send_message_in_chunks(message_reference, response_text)
            else:
                await send_message_in_chunks(channel, response_text)
        return

    # Build adventure HUD / choices if active
    if session and session.state.get("adventure_active"):
        embed = discord.Embed(
            title="📖 Dungeon Master's HUD",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(name="❤️ Health", value=session.state.get("player_health", "100/100"), inline=True)
        embed.add_field(name="⚡ Tension", value=f"{session.state.get('tension', 10)}/100", inline=True)
        
        loc = session.state.get("location")
        if loc:
            embed.add_field(name="📍 Location", value=loc, inline=False)

        inv_list = session.state.get("inventory", [])
        embed.add_field(name="🎒 Inventory", value=", ".join(inv_list) if inv_list else "*Empty*", inline=False)

        quests_list = session.state.get("quest_log", [])
        embed.add_field(name="📜 Active Quests", value="\n".join(f"- {q}" for q in quests_list) if quests_list else "*None*", inline=False)

        choices = session.state.get("choices", [])
        if choices:
            from bot.views import AdventureView
            view = AdventureView(
                choices=choices,
                user_id=user_id,
                session_id=session_id,
                runner=runner,
                artifact_service=artifact_service,
                session_service=session_service,
                update_state_fn=update_session_state,
                process_adventure_turn_fn=execute_agent_turn,
            )

    # Default: send text response
    if interaction:
        if response_text:
            await send_message_in_chunks(interaction.followup, response_text, embed=embed, view=view)
        elif embed:
            await interaction.followup.send(embed=embed, view=view)
    else:
        target = message_reference if message_reference else channel
        if response_text:
            await send_message_in_chunks(target, response_text, embed=embed, view=view)
        else:
            if message_reference and hasattr(message_reference, "reply"):
                await message_reference.reply(content="I processed your request but have no text response.", embed=embed, view=view)
            else:
                await channel.send(content="I processed your request but have no text response.", embed=embed, view=view)


@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages and DMs
    if message.author == client.user:
        return
    if not message.guild:
        return

    # Check if bot is explicitly mentioned
    is_mentioned = client.user in message.mentions

    # Bypassed mention/reply check if it is an active adventure thread
    is_adventure_thread = False
    if isinstance(message.channel, discord.Thread):
        try:
            temp_session = await session_service.get_session(
                app_name=APP_NAME, user_id=str(message.author.id), session_id=f"discord_{message.channel.id}"
            )
            is_adventure_thread = (
                temp_session 
                and temp_session.state.get("adventure_active")
                and message.channel.id in active_adventure_threads
            )
        except Exception:
            pass

    if not is_mentioned and not is_adventure_thread:
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

    # Route to isolated image session if replying to a cached image
    if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
        ref_msg = message.reference.resolved
        ref_meta = await get_image_metadata(str(ref_msg.id))
        if ref_meta and ref_meta.get("session_id"):
            session_id = ref_meta.get("session_id")
            logger.info("Routing reply to isolated image session: %s", session_id)

    if message.guild:
        from app.radio_state import register_channel_guild
        register_channel_guild(message.channel.id, message.guild.id)

    # Process message content
    msg_text = message.content.replace(f"<@{client.user.id}>", "").strip()
    
    if msg_text.lower() == "!reset":
        try:
            session_id = f"discord_{message.channel.id}"
            user_id = str(message.author.id)
            
            # Attempt to fetch the session first to clear any persistent user state
            try:
                session = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
                if session:
                    # Clear persistent user state keys
                    keys_to_delete = [k for k in session.state.keys() if k.startswith("user:")]
                    if keys_to_delete:
                        from google.adk.events import Event, EventActions
                        import time, uuid
                        dummy_event = Event(
                            timestamp=time.time(),
                            author="system",
                            invocation_id=f"state_wipe_{uuid.uuid4().hex[:8]}",
                            actions=EventActions(
                                state_delta={k: None for k in keys_to_delete}
                            ),
                        )
                        await session_service.append_event(session, dummy_event)
            except Exception:
                pass
                
            # Delete the session to clear history
            await session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
            
            # WORKAROUND FOR ADK BUG: delete_session does not cascade delete events!
            try:
                import sqlite3
                conn = sqlite3.connect('sessions.db')
                cursor = conn.cursor()
                cursor.execute("DELETE FROM events WHERE app_name = ? AND user_id = ? AND session_id = ?", (APP_NAME, user_id, session_id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Failed to clear orphaned events: {e}")
                
            await message.reply("🔄 Conversation history and agent state have been completely flushed!")
        except Exception as e:
            logger.exception("Error resetting session:")
            await message.reply(f"Failed to reset session: {e}")
        return
    


    # Process image attachments
    image_data = None
    if message.attachments:
        for attachment in message.attachments:
            result = await read_image_attachment(attachment)
            if result:
                image_data = result
                break
    elif message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
        ref_msg = message.reference.resolved
        if ref_msg.attachments:
            for attachment in ref_msg.attachments:
                result = await read_image_attachment(attachment)
                if result:
                    image_data = result
                    break

    # --- Restyle shortcut: "@bot restyle" while replying to any image ---
    if "restyle" in msg_text.lower():
        ref = message.reference
        if ref:
            # resolved is only populated if the message is cached — fetch it if not
            ref_msg = ref.resolved
            if ref_msg is None and ref.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(ref.message_id)
                except Exception as fetch_err:
                    logger.warning("Could not fetch referenced message for restyle: %s", fetch_err)
                    ref_msg = None

            if ref_msg and isinstance(ref_msg, discord.Message):
                # Check attachments (direct uploads) and embeds (links, other bots)
                IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
                image_found = any(
                    (a.content_type and a.content_type.startswith("image"))
                    or any(a.filename.lower().endswith(ext) for ext in IMAGE_EXTS)
                    for a in ref_msg.attachments
                ) or any(
                    e.image or e.thumbnail
                    for e in ref_msg.embeds
                )

                if image_found:
                    from bot.views import trigger_restyle_from_message
                    ref_meta = await get_image_metadata(str(ref_msg.id))
                    if ref_meta and ref_meta.get("prompt"):
                        original_prompt = ref_meta["prompt"]
                    else:
                        original_prompt = (
                            ref_msg.content.strip()
                            or (ref_msg.attachments[0].filename.rsplit(".", 1)[0] if ref_msg.attachments else "image")
                        )

                    await trigger_restyle_from_message(
                        message=message,
                        ref_msg=ref_msg,
                        original_prompt=original_prompt,
                        user_id=user_id,
                        session_id=session_id,
                        runner=runner,
                        artifact_service=artifact_service,
                        session_service=session_service,
                        update_state_fn=update_session_state,
                    )
                    return  # skip the normal agent turn

    try:
        await execute_agent_turn(
            channel=message.channel,
            author=message.author,
            content=msg_text,
            user_id=user_id,
            session_id=session_id,
            message_reference=message,
            image_data=image_data,
        )
    except Exception as e:
        logger.exception("Error running ADK agent:")
        try:
            await message.reply(f"An error occurred while processing your request: {e}")
        except Exception:
            pass



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
