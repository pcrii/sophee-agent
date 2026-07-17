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
from bot.history import trim_session_history, _db_clear_events
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
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("google_adk").setLevel(logging.DEBUG)
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

from app.agent import art_director, dj_agent
art_director_runner = Runner(
    agent=art_director,
    app_name=APP_NAME,
    session_service=session_service,
    artifact_service=artifact_service,
)

dj_agent_runner = Runner(
    agent=dj_agent,
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


@client.event
async def on_interaction(interaction: discord.Interaction):
    """Fallback handler for button interactions when no live View instance is registered.

    This fires for proc_* custom_ids from ProcessedImageView after a bot restart,
    allowing those buttons to keep working across restarts.
    """
    # Let discord.py handle it normally first if a view is registered
    if interaction.type != discord.InteractionType.component:
        return
    if interaction.response.is_done():
        return

    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("proc_"):
        return  # Not ours to handle

    try:
        parts = custom_id.split(":")
        action = parts[0]  # proc_reroll, proc_process, proc_filters, proc_useref

        if action == "proc_reroll":
            # proc_reroll:{source_msg_id}:{mode}:{user_id}:{session_id}
            source_msg_id, mode, user_id, session_id = int(parts[1]), parts[2], parts[3], parts[4]
        elif action in ("proc_process", "proc_filters", "proc_useref"):
            # proc_{action}:{source_msg_id}:{user_id}:{session_id}
            source_msg_id, user_id, session_id = int(parts[1]), parts[2], parts[3]
            mode = None
        else:
            return

        try:
            source_msg = await interaction.channel.fetch_message(source_msg_id)
        except Exception:
            await interaction.response.send_message("❌ Original image no longer accessible.", ephemeral=True)
            return

        from bot.views import PostProcessView, FiltersView

        if action == "proc_reroll":
            view = PostProcessView(source_msg, user_id, session_id, update_session_state, session_service)
            await view._apply_and_post(interaction, mode)

        elif action == "proc_process":
            view = PostProcessView(source_msg, user_id, session_id, update_session_state, session_service)
            await interaction.response.send_message("Choose a processing step:", view=view, ephemeral=True)

        elif action == "proc_filters":
            view = FiltersView(source_msg, user_id, session_id, update_session_state, session_service)
            await interaction.response.send_message("Choose a filter:", view=view, ephemeral=True)

        elif action == "proc_useref":
            await interaction.response.defer(ephemeral=True, thinking=True)
            from bot.artifact_helpers import save_reference_image_from_message
            await save_reference_image_from_message(source_msg, user_id, session_id)
            await interaction.followup.send("✅ Image saved as reference — available even after session resets.", ephemeral=True)

    except Exception as e:
        logger.error("on_interaction fallback error (custom_id=%s): %s", custom_id, e)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong processing that button.", ephemeral=True)


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

def _guild_obj():
    """Returns the guild Object for slash command scoping, or None if not configured."""
    return discord.Object(id=DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None


@tree.command(name="image_settings", description="View and configure image generation defaults", guilds=[discord.Object(id=DISCORD_GUILD_ID)] if DISCORD_GUILD_ID else discord.utils.MISSING)
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


@tree.command(name="llm_settings", description="Configure the general assistant's creativity and thinking level", guilds=[discord.Object(id=DISCORD_GUILD_ID)] if DISCORD_GUILD_ID else discord.utils.MISSING)
async def cmd_llm_settings(interaction: discord.Interaction):
    """Opens the LLM settings panel (ephemeral). Only affects the general conversational agent."""
    user_id = str(interaction.user.id)
    session_id = f"discord_{interaction.channel_id}"
    session = await get_or_create_session(user_id, session_id)
    state = session.state if session else {}

    from bot.views import LLMSettingsView
    view = LLMSettingsView(state, update_session_state, user_id, session_id)
    embed = view._build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.context_menu(name="Debug LLM Generation", guilds=[discord.Object(id=DISCORD_GUILD_ID)] if DISCORD_GUILD_ID else discord.utils.MISSING)
async def context_debug_llm(interaction: discord.Interaction, message: discord.Message):
    """Context menu command to view LLM metadata for a message ephemerally."""
    if message.author.id != client.user.id:
        await interaction.response.send_message("I can only debug messages generated by me!", ephemeral=True)
        return

    from bot.cache import get_text_metadata
    metadata = await get_text_metadata(str(message.id))
    
    if metadata:
        config = metadata.get("config", {})
        clean_config = {k: v for k, v in config.items() if v is not None}
        
        import json
        config_str = json.dumps(clean_config, indent=2)
        if config_str == "{}":
            config_str = "No specific overrides (using Gemini defaults)"
            
        embed = discord.Embed(
            title="🔍 LLM Generation Metadata",
            description=f"You inspected [this message]({message.jump_url}). Here is the exact routing and configuration used:",
            color=0x00FF00,
        )
        embed.add_field(name="Agent Router", value=f"`{metadata.get('agent_name', 'unknown')}`", inline=False)
        
        history_len = metadata.get("history_length", 0)
        roles = metadata.get("history_roles", [])
        if history_len > 0:
            history_str = f"Payload contains `{history_len}` total interaction turns.\n**Sequence:** " + ", ".join([f"`{r}`" for r in roles])
        else:
            history_str = "No history payload detected. (Using Session Compaction or Stateless API)"
            
        embed.add_field(name="Context Scope", value=history_str, inline=False)
        embed.add_field(name="Configuration Payload", value=f"```json\n{config_str}\n```", inline=False)
        embed.set_footer(text=f"Timestamp: {metadata.get('timestamp')}")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("No metadata found for this message. It may be too old or not generated via the LLM.", ephemeral=True)


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
    active_runner=None,
):
    """Executes a single conversational agent turn and processes response artifacts (images, TTS).
    """
    session = await get_or_create_session(user_id, session_id)

    # Trim history before running agent
    await trim_session_history(session_service, APP_NAME, user_id, session_id)

    # Auto-restore reference image from artifact if session state lost it (e.g. manual clear or restart)
    try:
        from bot.artifact_helpers import restore_reference_image_to_session
        await restore_reference_image_to_session(user_id, session_id)
    except Exception as _ref_err:
        logger.debug("Reference image restore skipped: %s", _ref_err)

    # We will accumulate any state changes required for this turn
    state_updates = {}

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
                state_updates["latest_input_image_artifact"] = artifact_name
                logger.info("Set reference image artifact from reply: %s", artifact_name)


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
            # Grab the chunks of the single replied message as context
            chunked_context = await fetch_chunked_context(replied_msg)
            if chunked_context:
                msg_text = f"[The user is replying to this message from {replied_msg.author.display_name}:\n---\n{chunked_context}\n---\n]\n\n{msg_text}"

    # Process image attachments
    if image_data:
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
        state_updates["latest_input_image_mime"] = image_data["mime_type"]

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
    if full_text:
        parts.append(types.Part.from_text(text=full_text))
    elif not parts:
        parts.append(types.Part.from_text(text=" "))

    new_message = types.Content(role="user", parts=parts)

    # Track artifacts before the run
    before_keys = set(
        await artifact_service.list_artifact_keys(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    )

    response_text = ""
    active_runner = active_runner or runner
    async def _run_agent(is_retry=False):
        nonlocal response_text
        try:
            async for event in active_runner.run_async(
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
            error_str = str(e).lower()
            if not is_retry and ("interaction" in error_str or "not found" in error_str or "404" in error_str or "400" in error_str or "invalid_request" in error_str):
                logger.warning(f"Interactions API session likely expired or invalid ({e}). Resetting interaction state and retrying...")
                # Clear the server-side interaction pointer via the proper ADK pattern
                await update_session_state(user_id, session_id, {"_gemini_interaction_id": None})
                session.state.pop("_gemini_interaction_id", None)
                session.events.clear()
                # Clear local events from DB so ADK doesn't try to reconstruct stale history
                await _db_clear_events(APP_NAME, user_id, session_id)
                await _run_agent(is_retry=True)
            else:
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
        
        part_data = await artifact_service.load_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            filename=new_image_key,
            session_id=session_id,
        )
        # Determine correct file suffix from key name
        if new_image_key.endswith(".png"):
            temp_suffix = ".png"
        else:
            temp_suffix = ".jpeg"

        with tempfile.NamedTemporaryFile(delete=False, suffix=temp_suffix, mode="wb") as f:
            f.write(part_data.inline_data.data)
            temp_file_path = f.name
            
        if "preprocessed_" in new_image_key:
            from bot.views import ProcessedImageView
            import re
            
            mode_match = re.search(r"preprocessed_(.+?)_\d+", new_image_key)
            mode = mode_match.group(1) if mode_match else "unknown"
            
            # source_message_id will be patched after the message is sent
            view = ProcessedImageView(
                source_message_id=0,
                mode=mode,
                user_id=user_id,
                session_id=session_id,
                session_service=session_service,
                update_state_fn=update_session_state
            )
        else:
            from bot.views import ImageView
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

        # If this was a preprocessed artifact, patch the view's source_message_id now
        if "preprocessed_" in new_image_key:
            view.source_message_id = sent_msg.id
            await sent_msg.edit(view=view)

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

    async def _cache_metadata(msgs):
        if not msgs:
            return
        from bot.cache import save_text_metadata
        try:
            final_session = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
            if final_session and final_session.state.get("last_llm_metadata"):
                meta = final_session.state["last_llm_metadata"]
                await save_text_metadata(
                    str(msgs[0].id),
                    meta.get("agent_name", "unknown"),
                    meta.get("config", {}),
                    session_id=session_id
                )
        except Exception as e:
            logger.error("Failed to cache text metadata: %s", e)

    sent_msgs = []
    
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
                sent_msgs = await send_message_in_chunks(message_reference, response_text)
            else:
                sent_msgs = await send_message_in_chunks(channel, response_text)
        await _cache_metadata(sent_msgs)
        return

    # Default: send text response
    if interaction:
        if response_text:
            sent_msgs = await send_message_in_chunks(interaction.followup, response_text, embed=embed, view=view)
        elif embed:
            m = await interaction.followup.send(embed=embed, view=view)
            sent_msgs = [m]
    else:
        target = message_reference if message_reference else channel
        if response_text:
            sent_msgs = await send_message_in_chunks(target, response_text, embed=embed, view=view)
        else:
            if message_reference and hasattr(message_reference, "reply"):
                m = await message_reference.reply(content="I processed your request but have no text response.", embed=embed, view=view)
                sent_msgs = [m]
            else:
                m = await channel.send(content="I processed your request but have no text response.", embed=embed, view=view)
                sent_msgs = [m]
                
    await _cache_metadata(sent_msgs)




@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages and DMs
    if message.author == client.user:
        return
    if not message.guild:
        return

    # Check if bot is explicitly mentioned
    is_mentioned = client.user in message.mentions

    if not is_mentioned:
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
    session_id = f"discord_{message.channel.id}"  # Persistent channel session

    active_runner = runner
    # Route to isolated session if replying to a cached message
    if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
        ref_msg = message.reference.resolved
        from bot.cache import get_image_metadata, get_text_metadata
        ref_meta = await get_image_metadata(str(ref_msg.id))
        if ref_meta and ref_meta.get("session_id"):
            session_id = ref_meta.get("session_id")
            active_runner = art_director_runner
            logger.info("Routing reply to isolated image session: %s", session_id)
        else:
            text_meta = await get_text_metadata(str(ref_msg.id))
            if text_meta:
                if text_meta.get("session_id"):
                    session_id = text_meta.get("session_id")
                    logger.info("Routing reply to isolated text session: %s", session_id)
                agent_name = text_meta.get("agent_name")
                if agent_name == "dj_agent":
                    active_runner = dj_agent_runner
                    logger.info("Routing reply directly to dj_agent")
                elif agent_name == "art_director":
                    active_runner = art_director_runner
                    logger.info("Routing reply directly to art_director")

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
                await _db_clear_events(APP_NAME, user_id, session_id)
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
    elif message.reference and message.reference.message_id:
        # resolved is only populated if the message is cached — fetch it if not
        ref_msg = message.reference.resolved
        if ref_msg is None:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            except Exception as fetch_err:
                logger.warning("Could not fetch referenced message for image grab: %s", fetch_err)
                ref_msg = None
                
        if ref_msg and isinstance(ref_msg, discord.Message) and ref_msg.attachments:
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
            active_runner=active_runner,
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
