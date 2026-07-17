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
    """No-op. Session history is managed server-side by the Gemini Interactions API.
    Retention is determined by the Google AI Studio project settings (default 55 days).
    """
    pass

