"""Radio queue management tools for the Sophee DJ agent.

Uses the shared radio_state registry instead of sys.modules introspection.
"""

import logging
import random

from google.adk.tools import ToolContext

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

    # Try to get guild_id to clean up now_playing_cache
    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 0
    guild_id = resolve_guild_id(channel_id) or channel_id
    now_playing_cache.pop(guild_id, None)

    return {
        "status": "success",
        "message": "Radio station stopped. You can start a new one anytime.",
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
    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    guild_id = resolve_guild_id(channel_id) or channel_id
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

    session = tool_context.session

    # Save the new mode
    state["mode"] = mode_lower

    # Clear the upcoming tracks queue and candidate pool so the new algorithm takes effect immediately
    state["upcoming_tracks"] = []
    state["candidate_pool"] = []

    # Import bot helpers
    from bot.audio import persist_radio_state_helper, replenish_radio_queue
    
    # Try to resolve channel_id
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999

    # Run replenish_radio_queue to immediately fill the queue with 3 tracks matching the new algorithm
    try:
        await replenish_radio_queue(state)
    except Exception as e:
        logger.exception("Error replenishing queue during mode change:")

    # Persist state
    from bot.client import session_service
    import asyncio
    from app.radio_state import resolve_guild_id
    guild_id = resolve_guild_id(channel_id) or channel_id
    
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
    from bot.audio import persist_radio_state_helper
    from bot.client import session_service
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
    from bot.audio import persist_radio_state_helper
    from bot.client import session_service
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
    from app.radio_state import is_station_active, resolve_guild_id, save_hibernated_radio

    state = _get_radio_state(tool_context)
    if not state or not state.get("active"):
        return {
            "status": "error",
            "message": "No active radio broadcast found for this server.",
        }

    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    guild_id = resolve_guild_id(channel_id) or channel_id

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

