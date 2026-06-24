"""Audio playback engine for Sophee internet radio.

Handles song downloading via yt-dlp, voice channel playback,
DJ commentary generation, and automatic queue replenishment.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import os
import platform
import random
import tempfile

import discord
import yt_dlp
from google.adk.artifacts import FileArtifactService
from google.adk.runners import Runner
from google.genai import types

from app.agent import dj_agent
from app.radio_state import active_radios, now_playing_cache, get_discord_client
from app.tools import (
    fetch_lastfm_similar_tracks,
    fetch_lastfm_tag_tracks,
    get_user_favorites,
)
from app.ytmusic_tools import (
    search_ytmusic_track,
    generate_ytmusic_radio,
)

logger = logging.getLogger("sophee.bot.audio")

# Ensure cache directories exist
os.makedirs("song_cache", exist_ok=True)
os.makedirs("tts_cache", exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SONG_DURATION_SECONDS = 600
SONG_SEARCH_RESULT_LIMIT = 8
NO_DJ_SEGMENT_CHANCE = 0.18
MAX_CACHE_SIZE_MB = 500  # Evict oldest files when cache exceeds this
DEFAULT_VOLUME = 0.5

SEGMENT_TYPE_WEIGHTS = [
    ("segue", 34),
    ("micro_trivia", 20),
    ("theme_note", 15),
    ("station_id", 10),
    ("mood_check", 8),
    ("listener_mail", 5),
    ("field_report", 4),
    ("fake_ad", 4),
]

SEGMENT_LABELS = {
    "segue": "DJ Segue",
    "micro_trivia": "Micro Trivia",
    "theme_note": "Theme Note",
    "station_id": "Station ID",
    "mood_check": "Mood Check",
    "listener_mail": "Listener Mail",
    "field_report": "Field Report",
    "fake_ad": "Sponsor Read",
}

# ---------------------------------------------------------------------------
# Persistent Favorites
# ---------------------------------------------------------------------------

FAVORITES_FILE = "data/user_favorites.json"

def _load_user_favorites() -> dict:
    """Loads all user favorites from the JSON file."""
    os.makedirs("data", exist_ok=True)
    if os.path.exists(FAVORITES_FILE):
        try:
            with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load user favorites: %s", e)
    return {}

def _save_user_favorites(favorites: dict):
    """Saves all user favorites to the JSON file."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save user favorites: %s", e)

def get_user_favorites(user_id: str) -> dict:
    """Returns the favorites dictionary for a user."""
    favs = _load_user_favorites()
    return favs.get(user_id, {"liked_tracks": [], "liked_artists": []})

def add_user_favorite_track(user_id: str, artist: str, title: str):
    """Adds a track to the user's persistent favorites."""
    favs = _load_user_favorites()
    user_favs = favs.setdefault(user_id, {"liked_tracks": [], "liked_artists": []})

    if "liked_tracks" not in user_favs:
        user_favs["liked_tracks"] = []
    if "liked_artists" not in user_favs:
        user_favs["liked_artists"] = []

    track_entry = {"artist": artist, "title": title}
    # Avoid duplicate track entry
    if not any(t.get("artist", "").lower() == artist.lower() and t.get("title", "").lower() == title.lower() for t in user_favs["liked_tracks"]):
        user_favs["liked_tracks"].append(track_entry)

    # Also add artist to liked_artists if not present
    if artist.lower() not in [a.lower() for a in user_favs["liked_artists"]]:
        user_favs["liked_artists"].append(artist)

    _save_user_favorites(favs)


# ---------------------------------------------------------------------------
# Song cache eviction
# ---------------------------------------------------------------------------

def _evict_song_cache():
    """Evicts oldest cached songs when total size exceeds MAX_CACHE_SIZE_MB."""
    cache_dir = "song_cache"
    if not os.path.exists(cache_dir):
        return

    files = []
    total_size = 0
    for f in os.listdir(cache_dir):
        path = os.path.join(cache_dir, f)
        if os.path.isfile(path):
            stat = os.stat(path)
            files.append((path, stat.st_mtime, stat.st_size))
            total_size += stat.st_size

    max_bytes = MAX_CACHE_SIZE_MB * 1024 * 1024
    if total_size <= max_bytes:
        return

    # Sort by modification time (oldest first)
    files.sort(key=lambda x: x[1])

    evicted = 0
    for path, _, size in files:
        if total_size <= max_bytes:
            break
        try:
            os.remove(path)
            total_size -= size
            evicted += 1
        except Exception as e:
            logger.warning("Failed to evict %s: %s", path, e)

    if evicted:
        logger.info(
            "Evicted %d cached songs (%.1f MB remaining)",
            evicted, total_size / 1024 / 1024,
        )


# ---------------------------------------------------------------------------
# DJ Segments
# ---------------------------------------------------------------------------

def choose_segment_type(segment_history, fake_ad_count):
    """Weighted random segment type selection, avoiding repeats."""
    if random.random() < NO_DJ_SEGMENT_CHANCE:
        return None

    weighted_types = []
    previous_type = segment_history[-1] if segment_history else None

    for segment_type, weight in SEGMENT_TYPE_WEIGHTS:
        if segment_type == previous_type:
            continue
        if segment_type == "fake_ad" and fake_ad_count >= 1:
            continue
        weighted_types.extend([segment_type] * weight)

    if not weighted_types:
        return "segue"

    return random.choice(weighted_types)


def build_segment_prompt(
    segment_type, playlist_thesis, prev_track, curr_track,
    position, total_tracks, full_playlist_str,
):
    """Generates a detailed DJ commentary prompt for a segment type."""
    # Time-aware context
    now = datetime.datetime.now()
    time_context = f"Current time: {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d')}."

    shared_context = f"""You are at position {position} out of {total_tracks}.
{time_context}
The station theme is: '{playlist_thesis}'.
Previous track: {prev_track}
Next track: {curr_track}
Full playlist:
{full_playlist_str}"""

    prompts = {
        "segue": f"""{shared_context}
Write a 2-3 sentence transition into the next track. Avoid generic DJ filler. Include one specific connection: artist history, production detail, label/scene context, instrumentation, lyrical theme, or why the transition works musically.""",
        "micro_trivia": f"""{shared_context}
Write a 1-2 sentence micro-trivia break about either the previous or next track. Make it concrete and music-specific. Do not over-explain, and do not use phrases like "fun fact" or "did you know".""",
        "theme_note": f"""{shared_context}
Write a 2 sentence note explaining how the next track fits the station theme. Be specific about sound, history, mood, scene, or songwriting. Do not summarize the whole playlist.""",
        "station_id": f"""{shared_context}
Write a very short station ID before the next track. One sentence only. It should sound like Sophee on internet radio: dry, observant, and quietly strange, but still clear.""",
        "mood_check": f"""{shared_context}
Write a 1-2 sentence mood check describing the emotional or sonic turn the playlist is taking right now. Focus on texture, tempo, energy, or atmosphere. No hype-man language.""",
        "listener_mail": f"""{shared_context}
Write a tiny fake listener-mail segment: one invented listener question, then Sophee answers it while naturally cueing the next track. Keep it 2-3 sentences total. The question should be music-aware, not random.""",
        "field_report": f"""{shared_context}
Write a brief imaginary field report from the musical world around the next track: a studio, record shop, basement venue, radio booth, regional scene, or era. Keep it grounded in the playlist's theme and cue the next track in 2 sentences.""",
        "fake_ad": f"""{shared_context}
Write a 15-second fake sponsor read for an imaginary product. It should be dry, specific, and music-aware rather than wacky. Reference the previous track lightly, but do not explain the joke. No catchphrases, no screaming ad voice, no generic surreal product pileup. 1-2 sentences.""",
    }

    return prompts.get(segment_type, prompts["segue"])


# ---------------------------------------------------------------------------
# Song downloading
# ---------------------------------------------------------------------------

async def download_song_async(query):
    """Downloads a song via yt-dlp with caching and candidate reporting."""
    _evict_song_cache()

    file_hash = hashlib.md5(query.encode()).hexdigest()
    out_path = f"song_cache/{file_hash}"

    for ext in ["webm", "m4a", "mp3", "opus"]:
        if os.path.exists(f"{out_path}.{ext}"):
            return f"{out_path}.{ext}", ""

    home_dir = os.path.expanduser("~")
    if platform.system() == "Windows":
        deno_path = os.path.join(home_dir, ".deno", "bin", "deno.exe")
    else:
        deno_path = os.path.join(home_dir, ".deno", "bin", "deno")

    base_ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_path + ".%(ext)s",
        "default_search": "ytsearch",
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "js_runtimes": {"node": {}, "deno": {"path": deno_path}},
    }

    def is_probably_full_album(entry):
        title = (entry.get("title") or "").lower()
        album_markers = [
            "full album", "full lp", "complete album",
            "entire album", "whole album", "album stream",
        ]
        return any(marker in title for marker in album_markers)

    def is_reasonable_song_result(entry):
        duration = entry.get("duration")
        if duration is None:
            return False
        if duration > MAX_SONG_DURATION_SECONDS:
            return False
        if is_probably_full_album(entry):
            return False
        return True

    def get_candidate_score(entry):
        title = (entry.get("title") or "").lower()
        score = 0
        
        # High priority for lyric videos
        if "lyric" in title or "lyrics" in title:
            score += 15
            
        # High priority for official audio-only versions
        if "official audio" in title or "audio stream" in title:
            score += 10
            
        # Moderate priority for "audio" mentions without "video"
        if "audio" in title and "video" not in title:
            score += 8
            
        # Deprioritize official music videos, visualizers, live versions, etc.
        if "music video" in title or "official video" in title or "official music video" in title:
            score -= 10
        if "live" in title:
            score -= 5
        if "visualizer" in title:
            score -= 3
            
        return score

    def extract():
        search_opts = {
            **base_ydl_opts,
            "skip_download": True,
            "extract_flat": True,
        }
        search_opts.pop("match_filter", None)

        with yt_dlp.YoutubeDL(search_opts) as ydl:
            search_info = ydl.extract_info(
                f"ytsearch{SONG_SEARCH_RESULT_LIMIT}:{query}", download=False
            )
            entries = search_info.get("entries", []) if search_info else []

        # Score and filter candidates to prioritize lyric/audio videos
        scored_entries = []
        for entry in entries:
            if not entry or not is_reasonable_song_result(entry):
                continue
            score = get_candidate_score(entry)
            scored_entries.append((score, entry))
            
        # Stable sort descending by score
        scored_entries.sort(key=lambda x: x[0], reverse=True)

        candidates_report = []
        final_path = None

        # Build candidates report and try downloading the best rated candidate first
        for score, entry in scored_entries:
            title = entry.get("title", "Unknown title")
            duration = entry.get("duration")
            url = entry.get("webpage_url") or entry.get("url", "No URL")
            if not entry.get("webpage_url") and entry.get("id"):
                url = f"https://www.youtube.com/watch?v={entry['id']}"

            if final_path is None:
                video_url = entry.get("webpage_url")
                if not video_url and entry.get("id"):
                    video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                if not video_url:
                    video_url = entry.get("url")

                if not video_url:
                    candidates_report.append(
                        f"\u274c **[REJECTED]** `{title}` - Missing URL\n\U0001f517 {url}"
                    )
                    continue

                try:
                    with yt_dlp.YoutubeDL(base_ydl_opts) as ydl:
                        downloaded_info = ydl.extract_info(video_url, download=True)
                        f_path = ydl.prepare_filename(downloaded_info)

                        if os.path.exists(f_path):
                            final_path = f_path
                        else:
                            for ext in ["webm", "m4a", "mp3", "opus"]:
                                candidate_path = f"{out_path}.{ext}"
                                if os.path.exists(candidate_path):
                                    final_path = candidate_path
                                    break

                    if final_path:
                        candidates_report.append(
                            f"\u2705 **[SELECTED & DOWNLOADED]** `{title}` ({duration}s) (Score: {score})\n\U0001f517 {url}"
                        )
                    else:
                        candidates_report.append(
                            f"\u26a0\ufe0f **[DOWNLOAD FAILED]** `{title}` ({duration}s) - File not found\n\U0001f517 {url}"
                        )
                except Exception as e:
                    logger.warning("Failed to download candidate %s: %s", video_url, e)
                    err_msg = str(e).split("\n")[0][:50]
                    candidates_report.append(
                        f"\u26a0\ufe0f **[DOWNLOAD FAILED]** `{title}` ({duration}s) - {err_msg}\n\U0001f517 {url}"
                    )
            else:
                candidates_report.append(
                    f"\u23ed\ufe0f **[SKIPPED]** `{title}` ({duration}s) (Score: {score}) - Alternative option\n\U0001f517 {url}"
                )

        # Include rejected entries that didn't pass is_reasonable_song_result for transparency
        for entry in entries:
            if not entry or is_reasonable_song_result(entry):
                continue
            title = entry.get("title", "Unknown title")
            duration = entry.get("duration")
            url = entry.get("webpage_url") or entry.get("url", "No URL")
            if not entry.get("webpage_url") and entry.get("id"):
                url = f"https://www.youtube.com/watch?v={entry['id']}"

            if duration is None:
                candidates_report.append(
                    f"\u274c **[REJECTED]** `{title}` - Unknown duration\n\U0001f517 {url}"
                )
            elif duration > MAX_SONG_DURATION_SECONDS:
                candidates_report.append(
                    f"\u274c **[REJECTED]** `{title}` ({duration}s) - Exceeds max duration\n\U0001f517 {url}"
                )
            elif is_probably_full_album(entry):
                candidates_report.append(
                    f"\u274c **[REJECTED]** `{title}` ({duration}s) - Flagged as full album\n\U0001f517 {url}"
                )

        if not final_path:
            logger.warning(
                "No suitable YouTube result found for '%s' under %ds.",
                query, MAX_SONG_DURATION_SECONDS,
            )
            return None, "\n".join(candidates_report)

        return final_path, "\n".join(candidates_report)

    loop = asyncio.get_running_loop()
    try:
        final_path, report = await loop.run_in_executor(None, extract)
        return final_path, report
    except Exception as e:
        logger.error("yt-dlp error for '%s': %s", query, e)
        return None, ""


# ---------------------------------------------------------------------------
# Radio persistence helpers
# ---------------------------------------------------------------------------

async def persist_radio_state_helper(guild_id: int, session_service, channel_id: int, state: dict):
    """Saves the current active radio state to the SQLite database session state."""
    try:
        user_id = state.get("user_id", "system")
        session_id = f"discord_{channel_id}"
        session = await session_service.get_session(
            app_name="app", user_id=user_id, session_id=session_id
        )
        if session:
            clean_state = {
                "active": state.get("active"),
                "playlist_thesis": state.get("playlist_thesis"),
                "genre": state.get("genre"),
                "upcoming_tracks": state.get("upcoming_tracks"),
                "played_tracks": state.get("played_tracks"),
                "current_track": state.get("current_track"),
                "liked_tracks": state.get("liked_tracks", []),
                "disliked_tracks": state.get("disliked_tracks", []),
                "mode": state.get("mode"),
                "seed_tags": state.get("seed_tags", []),
                "user_id": user_id,
                "voice_channel_id": state.get("voice_channel_id"),
                "text_channel_id": state.get("text_channel_id"),
                "use_dj": state.get("use_dj"),
            }
            session.state["active_radio"] = clean_state

            from google.adk.events import Event, EventActions
            import time
            import uuid
            dummy_event = Event(
                timestamp=time.time(),
                author="system",
                invocation_id=f"rad_update_{uuid.uuid4().hex[:8]}",
                actions=EventActions(state_delta={"active_radio": clean_state}),
            )
            await session_service.append_event(session, dummy_event)
            logger.debug("Radio state persisted to database for guild %s", guild_id)
    except Exception as e:
        logger.warning("Failed to persist radio state for guild %s: %s", guild_id, e)


async def clear_radio_state_helper(guild_id: int, session_service, channel_id: int):
    """Clears the active radio state from the SQLite database session state."""
    try:
        session_id = f"discord_{channel_id}"
        sessions_response = await session_service.list_sessions(app_name="app")
        target_session = None
        for s in sessions_response.sessions:
            if s.id == session_id:
                target_session = s
                break

        if target_session:
            target_session.state["active_radio"] = None
            from google.adk.events import Event, EventActions
            import time
            import uuid
            dummy_event = Event(
                timestamp=time.time(),
                author="system",
                invocation_id=f"rad_clear_{uuid.uuid4().hex[:8]}",
                actions=EventActions(state_delta={"active_radio": None}),
            )
            await session_service.append_event(target_session, dummy_event)
            logger.debug("Radio state cleared in database for guild %s", guild_id)
    except Exception as e:
        logger.warning("Failed to clear radio state for guild %s: %s", guild_id, e)


# ---------------------------------------------------------------------------
# Audio player task
# ---------------------------------------------------------------------------

async def audio_player_task(vc, queue, channel, abort_event):
    """Async consumer that plays audio items from a queue in a voice channel."""
    from bot.views import SkipView

    try:
        while True:
            if abort_event.is_set():
                break

            item = await queue.get()
            if item is None or abort_event.is_set():
                break

            try:
                if len(item) == 3:
                    file_path, label, report = item
                else:
                    file_path, label = item
                    report = ""

                if file_path and os.path.exists(file_path):
                    await channel.send(
                        f"\U0001f4fb Now Playing: **{label}**",
                        view=SkipView(vc, queue, abort_event),
                    )

                    if report:
                        try:
                            with open("yt_search_candidates.log", "a", encoding="utf-8") as f:
                                f.write(f"\n--- SEARCH CANDIDATES FOR: {label} ---\n")
                                f.write(report + "\n")
                        except Exception as e:
                            logger.warning("Failed to write candidate log: %s", e)

                    if hasattr(channel, "guild") and channel.guild:
                        now_playing_cache[channel.guild.id] = label

                    # Update voice channel status for songs (not TTS segments)
                    if "tts_cache" not in file_path and "artifacts" not in file_path:
                        try:
                            status_text = f"Now Playing: {label}"[:170]
                            if hasattr(vc.channel, "edit"):
                                await vc.channel.edit(status=status_text)
                        except Exception as e:
                            logger.debug("Failed to update VC status: %s", e)

                    play_event = asyncio.Event()

                    def after_play(error, event=play_event):
                        if error:
                            logger.error("Player error: %s", error)
                        vc.loop.call_soon_threadsafe(event.set)

                    # Volume control via PCMVolumeTransformer
                    source = discord.FFmpegPCMAudio(file_path)
                    source = discord.PCMVolumeTransformer(source, volume=DEFAULT_VOLUME)
                    
                    try:
                        vc.play(source, after=after_play)
                        await play_event.wait()
                    except discord.errors.ClientException as ce:
                        logger.error("ClientException during voice playback: %s", ce)
                        # Check if reconnecting
                        reconnect_wait = 0
                        while not vc.is_connected() and reconnect_wait < 10 and not abort_event.is_set():
                            await asyncio.sleep(1)
                            reconnect_wait += 1
                        
                        if vc.is_connected() and not abort_event.is_set():
                            logger.info("Voice client reconnected. Retrying playback...")
                            try:
                                source = discord.FFmpegPCMAudio(file_path)
                                source = discord.PCMVolumeTransformer(source, volume=DEFAULT_VOLUME)
                                vc.play(source, after=after_play)
                                await play_event.wait()
                            except Exception as retry_err:
                                logger.error("Failed retry of voice playback: %s", retry_err)
                        else:
                            logger.error("Voice client failed to reconnect. Stopping player task.")
                            break

                    # Cleanup temporary generated voice files
                    if "tts_cache" in file_path or "artifacts" in file_path:
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
                else:
                    await channel.send(f"\u26a0\ufe0f Could not load audio for: {label}")
            except Exception as loop_err:
                logger.exception("Error in audio_player_task loop: %s", loop_err)
            finally:
                queue.task_done()
    finally:
        logger.info("Exiting audio_player_task, cleaning up radio state.")
        abort_event.set()
        if hasattr(channel, "guild") and channel.guild:
            g_id = channel.guild.id
            now_playing_cache.pop(g_id, None)
            if g_id in active_radios:
                active_radios[g_id]["active"] = False
                from bot.client import session_service
                await clear_radio_state_helper(g_id, session_service, channel.id)

        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                logger.warning("Failed to disconnect voice client on exit: %s", e)

    await channel.send("\U0001f4fb Broadcast finished! Disconnecting.")


# ---------------------------------------------------------------------------
# Queue replenishment
# ---------------------------------------------------------------------------

async def jit_replenish_queue(state, channel=None):
    """Auto-fills the queue to maintain exactly 3 upcoming tracks using JIT scoring."""
    if len(state["upcoming_tracks"]) >= 3:
        return

    needed = 3 - len(state["upcoming_tracks"])
    if needed <= 0:
        return

    thesis = state.get("playlist_thesis", "music")
    genre = state.get("genre", thesis)
    mode = state.get("mode", "standard")
    seed_tags = state.get("seed_tags", [])
    user_id = state.get("user_id", "default_user")

    # Load persistent favorites
    favs = get_user_favorites(user_id)
    fav_tracks = favs.get("liked_tracks", [])
    fav_artists = {a.lower().strip() for a in favs.get("liked_artists", [])}

    liked_tracks = state.get("liked_tracks", [])
    disliked_tracks = state.get("disliked_tracks", [])
    played_tracks = state.get("played_tracks", [])

    liked_artists = {t["artist"].lower().strip() for t in liked_tracks}
    disliked_artists = {t["artist"].lower().strip() for t in disliked_tracks}

    added_tracks = []
    
    # Initialize rolling candidate pool
    if "candidate_pool" not in state:
        state["candidate_pool"] = []

    for _ in range(needed):
        # Age existing candidates
        for c in state["candidate_pool"]:
            c["age"] += 1
            
        # Purge candidates older than 10
        state["candidate_pool"] = [c for c in state["candidate_pool"] if c["age"] <= 10]

        new_candidates = []

        # Gather new candidates based on mode
        if mode == "discovery_favorites":
            if fav_tracks:
                sample_favs = random.sample(fav_tracks, min(len(fav_tracks), 2))
                for ft in sample_favs:
                    similar = await fetch_lastfm_similar_tracks(ft["artist"], ft["title"], limit=20)
                    for track in similar:
                        new_candidates.append((track, 15))
            else:
                pop_tracks = await fetch_lastfm_tag_tracks("pop", limit=20)
                for track in pop_tracks:
                    new_candidates.append((track, 2))

        elif mode == "discovery_genre":
            if seed_tags:
                for tag_entry in seed_tags:
                    if isinstance(tag_entry, dict):
                        tag_name = tag_entry.get("tag", genre)
                        tag_weight = float(tag_entry.get("weight", 1.0))
                    else:
                        tag_name = str(tag_entry)
                        tag_weight = 1.0

                    tag_tracks = await fetch_lastfm_tag_tracks(tag_name, limit=20)
                    for idx, track in enumerate(tag_tracks):
                        popularity_penalty = -8 if idx < 5 else 0
                        new_candidates.append((track, (10 * tag_weight) + popularity_penalty))
            else:
                tag_tracks = await fetch_lastfm_tag_tracks(genre, limit=20)
                for idx, track in enumerate(tag_tracks):
                    popularity_penalty = -8 if idx < 5 else 0
                    new_candidates.append((track, 10 + popularity_penalty))

            if liked_tracks:
                sample_liked = random.sample(liked_tracks, min(len(liked_tracks), 2))
                for lt in sample_liked:
                    similar = await fetch_lastfm_similar_tracks(lt["artist"], lt["title"], limit=15)
                    for track in similar:
                        new_candidates.append((track, 12))

        else:  # "standard"
            if liked_tracks:
                sample_liked = random.sample(liked_tracks, min(len(liked_tracks), 2))
                for lt in sample_liked:
                    similar = await fetch_lastfm_similar_tracks(lt["artist"], lt["title"], limit=20)
                    for track in similar:
                        new_candidates.append((track, 15))

            if played_tracks:
                # Randomly pick 2 influencers from the last 5 tracks
                influencers = random.sample(played_tracks[-5:], min(len(played_tracks), 2))
                for infl in influencers:
                    # Filter out actively disliked
                    is_infl_disliked = any(
                        t.get("artist", "").lower().strip() == infl.get("artist", "").lower().strip()
                        and t.get("title", "").lower().strip() == infl.get("title", "").lower().strip()
                        for t in disliked_tracks
                    )
                    if not is_infl_disliked:
                        # 1. Last.fm similarity
                        similar = await fetch_lastfm_similar_tracks(infl["artist"], infl["title"], limit=15)
                        for track in similar:
                            new_candidates.append((track, 10))
                            
                        # 2. YT Music similarity (needs videoId)
                        vid = infl.get("videoId")
                        if not vid:
                            yt_res = await search_ytmusic_track(f"{infl.get('artist')} {infl.get('title')}")
                            if yt_res.get("status") == "success":
                                vid = yt_res.get("videoId")
                                infl["videoId"] = vid 
                                
                        if vid:
                            yt_radio = await generate_ytmusic_radio(vid)
                            if yt_radio.get("status") == "success":
                                for track in yt_radio.get("tracks", [])[:15]:
                                    new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 12))

            if seed_tags:
                for tag_entry in seed_tags:
                    if isinstance(tag_entry, dict):
                        tag_name = tag_entry.get("tag", genre)
                        tag_weight = float(tag_entry.get("weight", 1.0))
                    else:
                        tag_name = str(tag_entry)
                        tag_weight = 1.0

                    tag_tracks = await fetch_lastfm_tag_tracks(tag_name, limit=20)
                    for track in tag_tracks:
                        new_candidates.append((track, 8 * tag_weight))
            else:
                tag_tracks = await fetch_lastfm_tag_tracks(genre, limit=20)
                for track in tag_tracks:
                    new_candidates.append((track, 5))

        if not new_candidates and not state["candidate_pool"]:
            pop_tracks = await fetch_lastfm_tag_tracks("pop", limit=20)
            for track in pop_tracks:
                new_candidates.append((track, 2))

        # Add new candidates to the pool
        for track, base_score in new_candidates:
            state["candidate_pool"].append({
                "track": track,
                "base_score": base_score,
                "age": 0
            })
            
        # Cap pool size at 100 to prevent memory bloat (sort by age, oldest first, and slice)
        if len(state["candidate_pool"]) > 100:
            state["candidate_pool"].sort(key=lambda x: x["age"])
            state["candidate_pool"] = state["candidate_pool"][:100]

        if not state["candidate_pool"]:
            break

        # Compute scoring for each unique candidate in the pool
        scored_candidates = []
        seen = set()

        for c_data in state["candidate_pool"]:
            track = c_data["track"]
            base_score = c_data["base_score"]
            age = c_data["age"]
            
            artist = str(track.get("artist", "")).strip()
            title = str(track.get("title", "")).strip()
            if not artist or not title:
                continue

            key = (artist.lower(), title.lower())
            if key in seen:
                continue
            seen.add(key)

            # Check duplication against upcoming, played, and currently added
            is_dup = any(
                t.get("artist", "").lower().strip() == key[0]
                and t.get("title", "").lower().strip() == key[1]
                for t in state["upcoming_tracks"] + played_tracks + added_tracks
            )
            if is_dup:
                continue

            # Check if disliked in current session
            is_disliked_track = any(
                t.get("artist", "").lower().strip() == key[0]
                and t.get("title", "").lower().strip() == key[1]
                for t in disliked_tracks
            )
            if is_disliked_track:
                continue

            # Exclude persistent favorites in discovery modes
            if mode in ["discovery_genre", "discovery_favorites"]:
                is_favorited = any(
                    t.get("artist", "").lower().strip() == key[0]
                    and t.get("title", "").lower().strip() == key[1]
                    for t in fav_tracks
                )
                if is_favorited:
                    continue

            score = base_score - (age * 0.5)

            if artist.lower() in liked_artists:
                score += 10
            if artist.lower() in disliked_artists:
                score -= 30
            if mode == "discovery_favorites" and artist.lower() in fav_artists:
                score -= 15

            scored_candidates.append((score, c_data))

        if not scored_candidates:
            # All candidates were filtered out (duplicates/dislikes). Clear pool and break to retry next tick.
            state["candidate_pool"] = []
            break

        # Sort descending by score
        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        best_score = scored_candidates[0][0]
        top_candidates = [c_data for score, c_data in scored_candidates if score == best_score]
        chosen_c_data = random.choice(top_candidates)

        chosen_track = chosen_c_data["track"]
        added_tracks.append(chosen_track)
        state["upcoming_tracks"].append(chosen_track)
        
        # Remove the chosen candidate from the pool
        try:
            state["candidate_pool"].remove(chosen_c_data)
        except ValueError:
            pass

    if added_tracks:
        logger.info("Replenished queue JIT with %d tracks. Candidate pool size: %d", len(added_tracks), len(state["candidate_pool"]))
        if channel:
            try:
                tracks_list_str = "\n".join([f"- **{t.get('artist')}** - *{t.get('title')}*" for t in added_tracks])
                await channel.send(
                    f"\U0001f504 **Queue Replenished (JIT)** with new tracks:\n{tracks_list_str}"
                )
            except Exception as e:
                logger.warning("Failed to send queue replenishment notification: %s", e)


# ---------------------------------------------------------------------------
# Continuous radio sequence builder
# ---------------------------------------------------------------------------

async def build_radio_sequence(
    queue, use_dj, guild_id, session_service, artifact_service, channel_id, abort_event,
):
    """Main continuous radio loop — downloads songs, generates DJ segments, and queues them."""
    state = active_radios.get(guild_id)
    if not state:
        await queue.put(None)
        return

    segment_history = []
    fake_ad_count = 0

    # Create a dedicated artifact service for DJ TTS artifacts
    if artifact_service is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        artifacts_dir = os.path.join(project_root, "data", "artifacts")
        artifact_service = FileArtifactService(root_dir=artifacts_dir)

    dj_runner = Runner(
        agent=dj_agent,
        app_name="app",
        session_service=session_service,
        artifact_service=artifact_service,
    )

    # Clean up leftover tts files
    for f in os.listdir("tts_cache"):
        if f.endswith(".wav"):
            try:
                os.remove(os.path.join("tts_cache", f))
            except Exception:
                pass

    logger.info("Starting continuous radio loop for guild %s", guild_id)
    while state["active"] and not abort_event.is_set():
        try:
            # Get the Discord channel object
            disc_client = get_discord_client()
            disc_channel = disc_client.get_channel(channel_id) if disc_client else None

            await replenish_radio_queue(state, channel=disc_channel)
            await persist_radio_state_helper(guild_id, session_service, channel_id, state)

            while queue.qsize() >= 2:
                if abort_event.is_set() or not state["active"]:
                    return
                await asyncio.sleep(1)

            if not state["upcoming_tracks"]:
                await asyncio.sleep(2)
                continue

            track = state["upcoming_tracks"].pop(0)
            state["played_tracks"].append(track)

            t_curr = f"{track.get('artist')} - {track.get('title')}"
            state["current_track"] = t_curr
            i = len(state["played_tracks"]) - 1
            await persist_radio_state_helper(guild_id, session_service, channel_id, state)

            # Build sliding window for playlist context
            played_recent = state["played_tracks"][-3:-1]
            upcoming_next = state["upcoming_tracks"][:3]
            window_tracks = [*played_recent, track, *upcoming_next]

            full_playlist_str = ""
            for t in window_tracks:
                is_curr = (t == track)
                prefix = "\U0001f50a " if is_curr else "   "
                full_playlist_str += f"{prefix}{t.get('artist')} - {t.get('title')}\n"

            if use_dj:
                dj_prompt = ""
                label = "DJ Segment"

                if i == 0:
                    dj_prompt = (
                        f"Write a dense, informative 2-3 sentence intro welcoming listeners "
                        f"to the show. Instead of using filler adjectives, drop a piece of "
                        f"overarching trivia or historical context about the "
                        f"'{state['playlist_thesis']}' theme. You are about to play track 1: "
                        f"{t_curr}. Here is the visible playlist window for context:\n"
                        f"{full_playlist_str}\n\nFinally, call `generate_tts` to read your intro."
                    )
                    label = "DJ Intro"
                else:
                    t_prev_dict = state["played_tracks"][-2]
                    t_prev = f"{t_prev_dict.get('artist')} - {t_prev_dict.get('title')}"
                    segment_type = choose_segment_type(segment_history, fake_ad_count)
                    if segment_type:
                        prompt_instructions = build_segment_prompt(
                            segment_type, state["playlist_thesis"],
                            t_prev, t_curr, i + 1,
                            "ongoing continuous broadcast", full_playlist_str,
                        )
                        dj_prompt = (
                            f"{prompt_instructions}\n\nFinally, call `generate_tts` "
                            f"to read your commentary."
                        )
                        label = SEGMENT_LABELS.get(segment_type, "DJ Segue")
                        segment_history.append(segment_type)
                        if segment_type == "fake_ad":
                            fake_ad_count += 1

                if dj_prompt:
                    dj_session_id = f"radio_session_{channel_id}"

                    if not await session_service.get_session(
                        app_name="app", user_id="system", session_id=dj_session_id
                    ):
                        await session_service.create_session(
                            app_name="app", user_id="system", session_id=dj_session_id
                        )

                    async for _event in dj_runner.run_async(
                        user_id="system",
                        session_id=dj_session_id,
                        new_message=types.Content(
                            role="user", parts=[types.Part.from_text(text=dj_prompt)]
                        ),
                    ):
                        pass

                    dj_session = await session_service.get_session(
                        app_name="app", user_id="system", session_id=dj_session_id
                    )
                    latest_wav = (
                        dj_session.state.get("latest_tts_artifact")
                        if dj_session
                        else None
                    )

                    if latest_wav:
                        # Clear the artifact reference so it doesn't replay
                        if dj_session:
                            dj_session.state["latest_tts_artifact"] = None

                        artifact_data = await artifact_service.load_artifact(
                            app_name="app",
                            user_id="system",
                            filename=latest_wav,
                            session_id=dj_session_id,
                        )

                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".wav", dir="tts_cache"
                        ) as f:
                            f.write(artifact_data.inline_data.data)
                            temp_file_path = f.name

                        await queue.put((temp_file_path, label))

            # Use videoId if available, otherwise fallback to artist-title search
            vid = track.get("videoId")
            query = f"https://music.youtube.com/watch?v={vid}" if vid else t_curr
            s_file, report = await download_song_async(query)
            if s_file:
                await queue.put((s_file, t_curr, report))
        except Exception as loop_err:
            logger.exception("Error in build_radio_sequence loop: %s", loop_err)
            await asyncio.sleep(5)


    state["active"] = False
    await queue.put(None)
