"""Session history trimming to prevent unbounded token growth."""

import logging

logger = logging.getLogger("sophee.bot.history")

# Maximum number of conversational turns to keep in session history.
# Each 'turn' is roughly a user message + model response pair.
MAX_HISTORY_TURNS = 40


async def trim_session_history(session_service, app_name: str, user_id: str, session_id: str):
    """Trims conversation history to the most recent MAX_HISTORY_TURNS exchanges.

    Only conversation events are trimmed — session.state (radio queue, preferences,
    image settings, etc.) is unaffected.
    """
    try:
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if not session or not hasattr(session, 'events') or not session.events:
            return

        max_events = MAX_HISTORY_TURNS * 2  # user + model per turn
        if len(session.events) > max_events:
            trimmed_count = len(session.events) - max_events
            session.events = session.events[-max_events:]
            logger.info(
                "Trimmed %d events from session %s (kept last %d)",
                trimmed_count, session_id, max_events
            )
    except Exception as e:
        logger.warning("Failed to trim session history for %s: %s", session_id, e)
