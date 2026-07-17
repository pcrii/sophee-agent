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




