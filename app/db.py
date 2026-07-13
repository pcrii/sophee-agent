"""Database session service for Sophee."""

from google.adk.sessions import DatabaseSessionService

# Initialize the session service used across the application
session_service = DatabaseSessionService(db_url="sqlite+aiosqlite:///sessions.db")
