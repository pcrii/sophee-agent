"""Session history trimming to prevent unbounded token growth."""

import asyncio
import logging
import os
import sqlite3
import time

logger = logging.getLogger("sophee.bot.history")

# Maximum number of conversational turns to keep in session history.
# We restored this to 40 turns because we now aggressively sanitize
# base64 images from the SQLite database after every turn, keeping the
# context window massive for text, but extremely cheap.
MAX_HISTORY_TURNS = 40

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sessions.db"
)


async def _db_clear_events(app_name: str, user_id: str, session_id: str):
    def _run():
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM events WHERE app_name = ? AND user_id = ? AND session_id = ?",
                (app_name, user_id, session_id)
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_run)


async def _db_trim_events(app_name: str, user_id: str, session_id: str, keep_limit: int):
    def _run():
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM events 
                WHERE app_name = ? AND user_id = ? AND session_id = ? 
                  AND id NOT IN (
                      SELECT id FROM events 
                      WHERE app_name = ? AND user_id = ? AND session_id = ? 
                      ORDER BY timestamp DESC LIMIT ?
                  )
                """,
                (app_name, user_id, session_id, app_name, user_id, session_id, keep_limit)
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_run)


async def sanitize_images_from_history(app_name: str, user_id: str, session_id: str):
    """Aggressively scrubs base64 image payloads from the session history in the DB.
    Replaces them with a text placeholder to preserve the flow of conversation
    without costing thousands of tokens on every subsequent turn.
    """
    import json
    def _run():
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, event_data FROM events WHERE app_name = ? AND user_id = ? AND session_id = ?",
                (app_name, user_id, session_id)
            )
            rows = cursor.fetchall()
            
            for row_id, event_data_str in rows:
                if "inline_data" not in event_data_str and "inlineData" not in event_data_str:
                    continue
                    
                try:
                    data = json.loads(event_data_str)
                    modified = False
                    
                    if "content" in data and "parts" in data["content"]:
                        for part in data["content"]["parts"]:
                            if "inline_data" in part or "inlineData" in part:
                                part.pop("inline_data", None)
                                part.pop("inlineData", None)
                                part["text"] = "[Image sanitized from history to save context budget]"
                                modified = True
                                
                    if modified:
                        cursor.execute(
                            "UPDATE events SET event_data = ? WHERE id = ?",
                            (json.dumps(data), row_id)
                        )
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_run)


async def trim_session_history(session_service, app_name: str, user_id: str, session_id: str):
    """Trims conversation history to the most recent MAX_HISTORY_TURNS exchanges,
    and flushes history entirely if the session has been inactive for more than 7 days.

    Only conversation events are trimmed — session.state (radio queue, preferences,
    image settings, etc.) is unaffected.
    """
    try:
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if not session:
            return

        # Check for 7-day inactivity flush (604800 seconds)
        now = time.time()
        
        # Aggressively scrub base64 image data from the ADK SQLite history database
        # to ensure that previous turns don't keep uploading images to Gemini!
        await sanitize_images_from_history(app_name, user_id, session_id)
        
        # Determine the time of the last activity by checking the last event's timestamp.
        # This is much safer than session.last_update_time which can be a datetime object or improperly updated.
        last_activity_time = 0
        if hasattr(session, 'events') and session.events:
            last_activity_time = session.events[-1].timestamp
            
        time_inactive = now - last_activity_time
        if last_activity_time > 0 and time_inactive > 604800:
            trimmed_count = len(session.events)
            session.events = []
            
            # WIPE server-side interactions history chain
            # The Interactions API caches the conversation on Google's servers.
            # If we don't delete this ID, ADK will pass it and resume the conversation from yesterday!
            keys_to_delete = [k for k in getattr(session, 'state', {}) if 'interaction' in k.lower()]
            for k in keys_to_delete:
                del session.state[k]

            await _db_clear_events(app_name, user_id, session_id)
            logger.info(
                "Flushed all %d history events from session %s due to inactivity (inactive for %.1fd)",
                trimmed_count, session_id, time_inactive / 86400.0
            )
            return

        # Otherwise perform standard event limit trimming
        if not hasattr(session, 'events') or not session.events:
            return

        max_events = MAX_HISTORY_TURNS * 2  # user + model per turn
        if len(session.events) > max_events:
            start_idx = len(session.events) - max_events
            safe_idx = -1
            
            # Walk forward first to find a safe user turn (author == 'user' and no function responses)
            for idx in range(start_idx, len(session.events)):
                event = session.events[idx]
                has_fr = False
                if event.content and event.content.parts:
                    has_fr = any(getattr(part, "function_response", None) is not None for part in event.content.parts)
                if event.author == "user" and not has_fr:
                    safe_idx = idx
                    break
                    
            # If not found walking forward, walk backward to find the nearest safe user turn
            if safe_idx == -1:
                for idx in range(start_idx - 1, -1, -1):
                    event = session.events[idx]
                    has_fr = False
                    if event.content and event.content.parts:
                        has_fr = any(getattr(part, "function_response", None) is not None for part in event.content.parts)
                    if event.author == "user" and not has_fr:
                        safe_idx = idx
                        break
                        
            if safe_idx != -1 and safe_idx > 0:
                keep_limit = len(session.events) - safe_idx
                trimmed_count = safe_idx
                session.events = session.events[safe_idx:]
                await _db_trim_events(app_name, user_id, session_id, keep_limit)
                logger.info(
                    "Trimmed %d events from session %s (kept last %d in DB and memory starting with safe user turn)",
                    trimmed_count, session_id, keep_limit
                )
            else:
                # Fallback to standard trimming if no safe index is found
                trimmed_count = len(session.events) - max_events
                session.events = session.events[-max_events:]
                await _db_trim_events(app_name, user_id, session_id, max_events)
                logger.info(
                    "Trimmed %d events from session %s (kept last %d in DB and memory via fallback)",
                    trimmed_count, session_id, max_events
                )
    except Exception as e:
        logger.warning("Failed to trim session history for %s: %s", session_id, e)

