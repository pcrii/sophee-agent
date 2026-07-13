"""Radio orchestration and persistence helpers extracted from bot."""

import logging
import asyncio
import os
import json
import random
from app.db import session_service
from app.ytmusic_tools import search_ytmusic_track, generate_ytmusic_radio

logger = logging.getLogger("sophee.app.radio_orchestration")

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


async def jit_replenish_queue(state, channel=None):
    """Auto-fills the queue to maintain exactly 3 upcoming tracks using JIT scoring."""
    if not state.get("jit_enabled", True):
        return

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

        # Only gather candidates if pool is low or 33% chance to stagger API calls
        import random
        if len(state["candidate_pool"]) < 30 or random.random() < 0.33:
            # Gather new candidates based on mode
            if mode == "ytm_native":
                # Purely driven by YouTube Music recommendations from recently played history
                if played_tracks:
                    infl = random.choice(played_tracks[-3:])
                    vid = infl.get("videoId")
                    if not vid:
                        yt_res = await search_ytmusic_track(f"{infl.get('artist')} {infl.get('title')}")
                        if yt_res.get("status") == "success":
                            vid = yt_res.get("videoId")
                            infl["videoId"] = vid
                    
                    if vid:
                        yt_radio = await generate_ytmusic_radio(vid)
                        if yt_radio.get("status") == "success":
                            for track in yt_radio.get("tracks", [])[:20]:
                                new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 15))
                else:
                    # If nothing has played yet, seed from the thesis via YTM search
                    yt_res = await search_ytmusic_track(genre)
                    if yt_res.get("status") == "success" and yt_res.get("videoId"):
                        yt_radio = await generate_ytmusic_radio(yt_res["videoId"])
                        if yt_radio.get("status") == "success":
                            for track in yt_radio.get("tracks", [])[:20]:
                                new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 15))

            elif mode == "strict_thesis":
                # Completely ignores recently played history. Purely driven by the LLM seed tags.
                tags_to_search = []
                if seed_tags:
                    for tag_entry in seed_tags:
                        if isinstance(tag_entry, dict):
                            tags_to_search.append((tag_entry.get("tag", genre), float(tag_entry.get("weight", 1.0))))
                        else:
                            tags_to_search.append((str(tag_entry), 1.0))
                else:
                    tags_to_search.append((genre, 1.0))

                import random
                tag_name, weight = random.choice(tags_to_search)
                yt_res = await search_ytmusic_track(tag_name)
                if yt_res.get("status") == "success" and yt_res.get("videoId"):
                    yt_radio = await generate_ytmusic_radio(yt_res["videoId"])
                    if yt_radio.get("status") == "success":
                        for track in yt_radio.get("tracks", [])[:15]:
                            new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 10 * weight))

            else:  # "standard" (Hybrid of history drift and thesis anchoring)
                import random
                if played_tracks:
                    infl = random.choice(played_tracks[-3:])
                    vid = infl.get("videoId")
                    if not vid:
                        yt_res = await search_ytmusic_track(f"{infl.get('artist')} {infl.get('title')}")
                        if yt_res.get("status") == "success":
                            vid = yt_res.get("videoId")
                            infl["videoId"] = vid 
                            
                    if vid:
                        yt_radio = await generate_ytmusic_radio(vid)
                        if yt_radio.get("status") == "success":
                            for track in yt_radio.get("tracks", [])[:10]:
                                new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 12))

                # Mix in the thesis tags to prevent total drift (but not every single time)
                if seed_tags and random.random() < 0.5:
                    import random
                    tag_entry = random.choice(seed_tags)
                    tag = tag_entry if not isinstance(tag_entry, dict) else tag_entry.get("tag", genre)
                    yt_res = await search_ytmusic_track(tag)
                    if yt_res.get("status") == "success" and yt_res.get("videoId"):
                        yt_radio = await generate_ytmusic_radio(yt_res["videoId"])
                        if yt_radio.get("status") == "success":
                            for track in yt_radio.get("tracks", [])[:10]:
                                new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 10))

            if not new_candidates and not state["candidate_pool"]:
                yt_res = await search_ytmusic_track("popular hits")
                if yt_res.get("status") == "success" and yt_res.get("videoId"):
                    yt_radio = await generate_ytmusic_radio(yt_res["videoId"])
                    if yt_radio.get("status") == "success":
                        for track in yt_radio.get("tracks", [])[:10]:
                            new_candidates.append(({"artist": track["artists"][0] if track["artists"] else "Unknown", "title": track["title"], "videoId": track["videoId"]}, 2))

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

            # Check duplication against upcoming, played, currently added, AND the display queue
            # (Tracks in the display queue are buffered for playback, so JIT must know about them)
            all_known = state["upcoming_tracks"] + played_tracks + added_tracks + state.get("display_queue", [])
            vid = str(track.get("videoId", "")).strip()
            
            is_dup = any(
                (t.get("artist", "").lower().strip() == key[0] and t.get("title", "").lower().strip() == key[1])
                or (vid and t.get("videoId") == vid)
                for t in all_known
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

            # Exclude persistent favorites in discovery_genre mode
            if mode == "discovery_genre":
                is_favorited = any(
                    t.get("artist", "").lower().strip() == key[0]
                    and t.get("title", "").lower().strip() == key[1]
                    for t in fav_tracks
                )
                if is_favorited:
                    continue

            score = base_score - (age * 0.5)

            # Sliding window penalty for Artist Dominance
            recent_artists = [t.get("artist", "").lower().strip() for t in played_tracks[-10:]]
            artist_count = recent_artists.count(artist.lower())
            if artist_count > 0:
                score -= (artist_count * 15)

            if artist.lower() in liked_artists:
                score += 10
            if artist.lower() in disliked_artists:
                score -= 30

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
            await _update_queue_card(state, channel)



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
