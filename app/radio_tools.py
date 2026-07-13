"""Radio queue management tools for the Sophee DJ agent.

Uses the shared radio_state registry instead of sys.modules introspection.
"""

import logging
import random

from google.adk.tools import ToolContext
from app.db import session_service

from app.radio_state import active_radios, now_playing_cache, resolve_guild_id
from app.tools import (
    fetch_lastfm_similar_artists_tracks,
    fetch_lastfm_tag_tracks,
    fetch_new_music_releases,
)

logger = logging.getLogger("sophee.app.radio_tools")


def _get_radio_state(tool_context: ToolContext) -> dict | None:
    """Resolves the active radio state for the current session's guild."""
    session = tool_context.session
    if not session or not session.id:
        return None

    session_id = session.id
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999  # Fallback for testing

    guild_id = resolve_guild_id(channel_id)
    if guild_id is None:
        guild_id = channel_id  # Best-effort fallback

    return active_radios.get(guild_id)


def _resolve_guild(tool_context: ToolContext) -> int:
    """Resolves the guild ID for the current session. Returns 9999 as a test fallback."""
    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    return resolve_guild_id(channel_id) or channel_id


async def stop_station(tool_context: ToolContext) -> dict:
    """Stops the currently running radio station and clears its state.
    Use this when the user asks to stop the radio, kill the station, or shut it down.

    Returns:
        A confirmation that the station was stopped.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "info",
            "message": "No active radio station to stop.",
        }

    state["active"] = False
    state["upcoming_tracks"] = []

    # Clean up now_playing_cache
    guild_id = _resolve_guild(tool_context)
    now_playing_cache.pop(guild_id, None)

    return {
        "status": "success",
        "message": "Radio station stopped. You can start a new one anytime.",
    }


async def configure_radio_settings(tool_context: ToolContext, mode: str = None, jit_enabled: bool = None) -> dict:
    """Views or modifies the radio settings for the current server.
    Use this when the user asks to change the radio mode (e.g. 'standard', 'ytm_native', 'strict_thesis'),
    turn the JIT auto-generator on or off, or wants to check their current settings.

    Args:
        mode: Optional. The radio curation mode. Valid options are 'standard', 'ytm_native', or 'strict_thesis'.
        jit_enabled: Optional. Whether the Just-In-Time (JIT) queue replenisher should automatically add songs.

    Returns:
        A dictionary containing the updated settings.
    """
    guild_id = _resolve_guild(tool_context)
    
    # Initialize state if it doesn't exist
    if guild_id not in active_radios:
        active_radios[guild_id] = {"active": False, "mode": "standard", "jit_enabled": True}
        
    state = active_radios[guild_id]
    
    updates_made = []
    if mode is not None:
        if mode in ["standard", "ytm_native", "strict_thesis"]:
            state["mode"] = mode
            updates_made.append(f"mode set to '{mode}'")
        else:
            return {"status": "error", "message": f"Invalid mode '{mode}'. Must be standard, ytm_native, or strict_thesis."}
            
    if jit_enabled is not None:
        state["jit_enabled"] = jit_enabled
        updates_made.append(f"JIT auto-gen set to {'ON' if jit_enabled else 'OFF'}")
        
    action_msg = "Updated settings: " + ", ".join(updates_made) if updates_made else "Current settings:"
        
    return {
        "status": "success",
        "message": action_msg,
        "current_mode": state.get("mode", "standard"),
        "jit_enabled": state.get("jit_enabled", True)
    }

async def open_radio_settings_menu(tool_context: ToolContext) -> dict:
    """Opens the visual Radio Settings UI menu in Discord.
    Use this when the user asks to see the radio settings menu, open the radio settings,
    or wants the visual UI to configure radio preferences like JIT auto-gen and modes.

    Returns:
        A confirmation that the menu was opened.
    """
    tool_context.state["show_radio_settings_embed"] = True
    return {
        "status": "success",
        "message": "Radio settings menu has been pushed to the user's screen."
    }

async def show_station_queue(tool_context: ToolContext) -> dict:
    """Shows the current radio station's queue — what's playing now and what's coming up.
    Use this when the user asks what's playing, what's next, what's in the queue, or wants to see the tracklist.

    Returns:
        A dictionary containing the current track and the list of upcoming tracks in order.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    upcoming = [
        {"index": idx + 1, "artist": track.get("artist"), "title": track.get("title")}
        for idx, track in enumerate(state.get("upcoming_tracks", []))
    ]

    # Use now_playing_cache for what's ACTUALLY playing in voice,
    # not state["current_track"] which is set when queued (ahead of playback).
    guild_id = _resolve_guild(tool_context)
    actually_playing = now_playing_cache.get(guild_id, state.get("current_track"))

    return {
        "status": "success",
        "now_playing": actually_playing,
        "upcoming_tracks": upcoming,
        "playlist_thesis": state.get("playlist_thesis"),
    }


async def remove_from_queue(index: int, tool_context: ToolContext) -> dict:
    """Removes a track from the upcoming queue at the specified 1-based index.
    Use this when the user asks to remove, delete, or skip a specific song from the upcoming list.

    Args:
        index: The 1-based index of the track in the upcoming queue to remove.

    Returns:
        A dictionary detailing the removed track.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    upcoming = state.get("upcoming_tracks", [])
    if index < 1 or index > len(upcoming):
        return {
            "status": "error",
            "message": f"Invalid queue index {index}. Queue has {len(upcoming)} songs.",
        }

    removed = upcoming.pop(index - 1)
    return {
        "status": "success",
        "message": f"Successfully removed '{removed.get('artist')} - {removed.get('title')}' from queue.",
        "removed_track": removed,
    }


async def add_to_queue(artist: str, title: str, tool_context: ToolContext, play_next: bool = False, video_id: str = None) -> dict:
    """Adds a track (artist and title) to the upcoming queue.
    Use this when the user requests to add, queue up, or append a specific track.
    Set play_next to true when the user says "play this next", "put this on next",
    or otherwise wants the track to play immediately after the current song.

    Args:
        artist: The name of the artist.
        title: The title of the song.
        play_next: If true, inserts at the top of the queue so it plays next.
                   If false (default), appends to the end of the queue.
        video_id: Optional YouTube videoId for the exact track audio. Provide this if you obtained it from search_ytmusic_track.

    Returns:
        A success message with the track details and position.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    new_track = {"artist": artist, "title": title, "is_request": True}
    if video_id:
        new_track["videoId"] = video_id
        
    upcoming = state.setdefault("upcoming_tracks", [])

    if play_next:
        upcoming.insert(0, new_track)
        position_msg = "at the top of the queue (playing next)"
    else:
        upcoming.append(new_track)
        position_msg = f"at position {len(upcoming)} in the queue"

    return {
        "status": "success",
        "message": f"Successfully added '{artist} - {title}' {position_msg}.",
        "added_track": new_track,
    }


async def shuffle_queue(tool_context: ToolContext) -> dict:
    """Shuffles the order of the tracks currently in the upcoming queue.
    Use this when the user asks to shuffle, mix up, or randomize the playlist queue.

    Returns:
        A success message and the new order of upcoming tracks.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    upcoming = state.get("upcoming_tracks", [])
    random.shuffle(upcoming)

    new_upcoming = [
        {"index": idx + 1, "artist": track.get("artist"), "title": track.get("title")}
        for idx, track in enumerate(upcoming)
    ]

    return {
        "status": "success",
        "message": "Successfully shuffled the upcoming queue.",
        "upcoming_tracks": new_upcoming,
    }


async def steer_radio(direction: str, tool_context: ToolContext) -> dict:
    """Steers the musical direction of the radio. Clears the upcoming queue and refills it
    with new candidate tracks based on the target direction.
    Use this when the user wants to steer the radio to a new genre, tag, vibe, or style,
    or if they request new releases.

    Args:
        direction: The new direction, genre, tag, artist, or vibe
                   (e.g., 'pop', 'synthwave', 'new releases', 'similar to Radiohead').

    Returns:
        A success message and the list of new upcoming tracks.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    # Clear upcoming tracks and the JIT candidate pool
    state["upcoming_tracks"] = []
    state["candidate_pool"] = [] # Consider all queued candidates influentially dead
    state["playlist_thesis"] = direction

    direction_lower = direction.lower().strip()
    new_tracks = []

    # Agentic Tag Expansion for discovery_genre mode
    mode = state.get("mode", "standard")
    if mode == "discovery_genre":
        import os
        from google import genai
        try:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            client = genai.Client(api_key=api_key)
            model_id = "gemini-3.1-flash-lite"
            prompt = f"""You are a music expert. The user wants to steer their discovery radio to: '{direction}'.
Generate a list of exactly 3-5 relevant, specific Last.fm tags that represent this sonic direction.

STRICT OUTPUT FORMAT (JSON ONLY, no markdown formatting):
{{
  "seed_tags": ["tag1", "tag2", "tag3"]
}}"""
            response = await client.aio.models.generate_content(model=model_id, contents=prompt)
            from app.tools import _extract_json
            data = _extract_json(response.text)
            seed_tags = data.get("seed_tags", [])
            state["seed_tags"] = seed_tags
        except Exception as e:
            logger.warning("Failed to expand tags during steer: %s", e)
            state["seed_tags"] = [direction]

    # FIX: was tautological — now checks both singular and plural
    if "new release" in direction_lower or "new releases" in direction_lower:
        genre_filter = ""
        for tag in [
            "pop", "rock", "rap", "hip hop", "jazz", "metal",
            "electronic", "synthwave", "indie", "ambient",
        ]:
            if tag in direction_lower:
                genre_filter = tag
                break
        new_tracks = await fetch_new_music_releases(genre=genre_filter)
        state["genre"] = genre_filter if genre_filter else "pop"
    elif "similar to" in direction_lower or "like" in direction_lower:
        artist_query = direction.replace("similar to", "").replace("like", "").strip()
        new_tracks = await fetch_lastfm_similar_artists_tracks(artist_query, limit=15)
        state["genre"] = direction
    else:
        state["genre"] = direction
        if mode == "discovery_genre" and state.get("seed_tags"):
            # Fetch candidates from all seed tags
            for tag in state["seed_tags"]:
                tracks = await fetch_lastfm_tag_tracks(tag, limit=15)
                new_tracks.extend(tracks)
        else:
            new_tracks = await fetch_lastfm_tag_tracks(direction, limit=15)

    if new_tracks:
        random.shuffle(new_tracks)
        state["upcoming_tracks"] = new_tracks[:3]
    else:
        logger.warning("No tracks found for direction '%s', queue empty", direction)

    upcoming = [
        {"index": idx + 1, "artist": track.get("artist"), "title": track.get("title")}
        for idx, track in enumerate(state.get("upcoming_tracks", []))
    ]

    return {
        "status": "success",
        "message": f"Radio successfully steered towards '{direction}'.",
        "upcoming_tracks": upcoming,
    }


async def change_radio_mode(mode: str, tool_context: ToolContext) -> dict:
    """Changes the curation algorithm/mode of the active radio station.
    Use this when the user asks to change, swap, or switch the radio algorithm, playlist algorithm,
    or playback mode (e.g., 'standard', 'ytm_native', or 'strict_thesis').

    Args:
        mode: The new mode to switch to ('standard', 'ytm_native', 'strict_thesis').

    Returns:
        A dictionary indicating the result of the mode change.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    mode_lower = mode.lower().strip()
    if mode_lower not in ["standard", "ytm_native", "strict_thesis"]:
        return {
            "status": "error",
            "message": f"Invalid mode '{mode}'. Mode must be one of: 'standard', 'ytm_native', 'strict_thesis'.",
        }

    # Save the new mode
    state["mode"] = mode_lower

    # Clear the upcoming tracks queue and candidate pool so the new algorithm takes effect immediately
    state["upcoming_tracks"] = []
    state["candidate_pool"] = []

    from app.radio_orchestration import persist_radio_state_helper, jit_replenish_queue

    # Run replenish_radio_queue to immediately fill the queue with 3 tracks matching the new algorithm
    try:
        await jit_replenish_queue(state, channel=None)
    except Exception as e:
        logger.exception("Error replenishing queue during mode change:")

    # Persist state
    from app.radio_orchestration import jit_replenish_queue, persist_radio_state_helper
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999

    asyncio.create_task(
        persist_radio_state_helper(guild_id, session_service, channel_id, state)
    )

    upcoming = [
        {"index": idx + 1, "artist": track.get("artist"), "title": track.get("title")}
        for idx, track in enumerate(state.get("upcoming_tracks", []))
    ]

    return {
        "status": "success",
        "message": f"Successfully swapped playlist algorithm to '{mode_lower}'. The upcoming queue has been regenerated.",
        "upcoming_tracks": upcoming,
    }


async def mutate_upcoming_queue(tool_context: ToolContext, chaotic: bool = False) -> dict:
    """Mutates the tracks currently in the upcoming queue by replacing each track with a
    randomly selected similar track from Last.fm.
    Use this when the user requests to mutate, randomize, inject randomness/chaos, reroll,
    or warp the active upcoming queue of the running station.

    Args:
        chaotic: If True, selects from a wider pool of similar tracks (limit=20) for more obscure/chaotic matches.
                 If False (default), selects from a tighter pool (limit=5) for smoother, closer vibes.

    Returns:
        A success message with the new mutated queue, or an error if no active station.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    upcoming = state.get("upcoming_tracks", [])
    if not upcoming:
        return {
            "status": "error",
            "message": "The upcoming queue is empty. There are no tracks to mutate.",
        }

    from app.tools import fetch_lastfm_similar_tracks
    import random

    pool_size = 20 if chaotic else 5
    mutated_tracks = []

    for track in upcoming:
        artist = track.get("artist", "")
        title = track.get("title", "")
        if not artist or not title:
            mutated_tracks.append(track)
            continue

        try:
            similar = await fetch_lastfm_similar_tracks(artist, title, limit=pool_size)
            if similar:
                chosen = random.choice(similar)
                mutated_tracks.append({
                    "artist": chosen.get("artist", "Unknown Artist"),
                    "title": chosen.get("title", "Unknown Title")
                })
                continue
        except Exception as e:
            logger.warning("Failed to mutate track '%s - %s': %s", artist, title, e)

        mutated_tracks.append(track)

    state["upcoming_tracks"] = mutated_tracks

    # Persist change
    from app.radio_orchestration import persist_radio_state_helper

    import asyncio
    from app.radio_state import resolve_guild_id

    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    guild_id = resolve_guild_id(channel_id) or channel_id

    asyncio.create_task(
        persist_radio_state_helper(guild_id, session_service, channel_id, state)
    )

    new_upcoming = [
        {"index": idx + 1, "artist": t.get("artist"), "title": t.get("title")}
        for idx, t in enumerate(state.get("upcoming_tracks", []))
    ]

    mode_text = "chaotic" if chaotic else "smooth"
    return {
        "status": "success",
        "message": f"Successfully mutated the upcoming queue ({mode_text} mode).",
        "upcoming_tracks": new_upcoming,
    }

async def toggle_radio_jit(enabled: bool, tool_context: ToolContext) -> dict:
    """Toggles Just-In-Time (JIT) algorithmic track generation for the radio station.
    When JIT is ON (True), the station acts like an endless algorithm, automatically finding new tracks 
    that match the vibe/seed when the queue runs low.
    When JIT is OFF (False), the station acts like a finite CD player. It will only play exactly what is 
    in the upcoming_tracks queue (e.g. playlists you load) and then stop.
    
    Args:
        enabled: True to enable endless algorithmic radio, False to disable it and only play manually queued tracks.

    Returns:
        A success message indicating the new JIT state.
    """
    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    state["jit_enabled"] = enabled

    # Persist change
    from app.radio_orchestration import persist_radio_state_helper

    import asyncio

    guild_id = _resolve_guild(tool_context)
    session_id = (tool_context.session.id if tool_context.session else "")
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999

    asyncio.create_task(
        persist_radio_state_helper(guild_id, session_service, channel_id, state)
    )

    mode_text = "ON (Endless algorithmic radio)" if enabled else "OFF (Finite playlist mode)"
    return {
        "status": "success",
        "message": f"Successfully toggled JIT generation to {mode_text}.",
    }

async def hibernate_radio(tool_context: ToolContext) -> dict:
    """Saves the current radio station state for later and stops the broadcast.
    Use this when the user asks to save, hibernate, or pause a station for later.
    
    Returns:
        A success message indicating the station was hibernated.
    """
    from app.radio_state import save_hibernated_radio

    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    guild_id = _resolve_guild(tool_context)

    # 1. Save it to disk
    success = save_hibernated_radio(guild_id)
    
    if not success:
        return {
            "status": "error",
            "message": "Failed to save the radio state to disk."
        }

    # 2. Stop the station
    await stop_station(tool_context)

    return {
        "status": "success",
        "message": "Successfully hibernated the radio station! It has been stopped, but you can resume it anytime.",
    }


async def resume_radio(tool_context: ToolContext) -> dict:
    """Resumes a previously hibernated radio station. Reconnects to the saved voice channel
    and restarts the broadcast from where it left off, using JIT to replenish the queue.
    Use this when the user asks to resume, restore, wake up, or continue a hibernated station.

    Returns:
        A success message if the station was resumed, or an error if no hibernated state exists.
    """
    import asyncio
    from app.radio_state import load_hibernated_radio, set_radio_state, get_discord_client, get_discord_callback
    from app.radio_orchestration import persist_radio_state_helper


    guild_id = _resolve_guild(tool_context)

    # Check nothing is already running
    from app.radio_state import is_station_active
    if is_station_active(guild_id):
        return {
            "status": "error",
            "message": "A radio station is already running. Stop it first before resuming a hibernated one.",
        }

    state = load_hibernated_radio(guild_id)
    if not state:
        return {
            "status": "error",
            "message": "No hibernated radio station found for this server.",
        }

    # Restore active flag
    state["active"] = True
    # Clear runtime queues — stale upcoming_tracks from old sessions can flood the
    # download queue and the queue card. JIT will refill from seed_tags immediately.
    state["candidate_pool"] = []
    state["upcoming_tracks"] = []
    state["display_queue"] = []
    state.pop("now_playing_message_id", None)
    state.pop("queue_display_message_id", None)
    set_radio_state(guild_id, state)

    # Reconnect to voice
    client = get_discord_client()
    if not client:
        return {"status": "error", "message": "Discord client not available."}

    voice_channel_id = state.get("voice_channel_id")
    text_channel_id = state.get("text_channel_id")
    if not voice_channel_id or not text_channel_id:
        return {"status": "error", "message": "Hibernated state is missing channel IDs."}

    voice_channel = client.get_channel(voice_channel_id)
    text_channel = client.get_channel(text_channel_id)
    if not voice_channel or not text_channel:
        return {"status": "error", "message": "Could not find the original voice or text channel."}

    guild = voice_channel.guild
    vc = guild.voice_client
    try:
        if vc and vc.is_connected():
            await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()
    except Exception as e:
        return {"status": "error", "message": f"Failed to connect to voice channel: {e}"}

    # Persist restored state to DB
    asyncio.create_task(
        persist_radio_state_helper(guild_id, session_service, text_channel_id, state)
    )

    use_dj = state.get("use_dj", True)
    abort_event = asyncio.Event()
    audio_queue = asyncio.Queue(maxsize=3)

    audio_player_task = get_discord_callback("audio_player_task")
    build_radio_sequence = get_discord_callback("build_radio_sequence")
    
    if not audio_player_task or not build_radio_sequence:
        return {
            "status": "error",
            "message": "Discord callbacks not registered. Cannot resume voice.",
        }

    task1 = asyncio.create_task(
        audio_player_task(vc, audio_queue, text_channel, abort_event)
    )
    task2 = asyncio.create_task(
        build_radio_sequence(
            audio_queue, state.get("use_dj", False), guild_id,
            session_service, None,
            text_channel_id, abort_event
        )
    )
    for task in (task1, task2):
        task.add_done_callback(lambda t: None)

    thesis = state.get("playlist_thesis", "your previous session")
    mode_text = "with DJ commentary" if use_dj else "in pure music mode"
    return {
        "status": "success",
        "message": f"Resuming **{thesis}** station {mode_text}. JIT will replenish the queue momentarily.",
    }

