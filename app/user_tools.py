"""User preference and learning tools.

These tools allow the bot to remember per-user behavioral corrections
and preferences. Stored in session state and persisted via DatabaseSessionService.
"""

import logging

from google.adk.tools import ToolContext

logger = logging.getLogger("sophee.app.user_tools")

# Maximum number of corrections to store per user
MAX_CORRECTIONS = 20


async def remember_preference(preference: str, tool_context: ToolContext) -> dict:
    """Saves a user behavioral preference or correction for all future interactions.
    Call this when the user tells you to change how you behave, respond, or communicate.

    Args:
        preference: What to remember (e.g. 'User prefers concise responses',
                    'User dislikes emoji', 'User wants deep cuts over radio hits').

    Returns:
        A confirmation that the preference was saved.
    """
    prefs = tool_context.state.get("user_prefs", {"corrections": [], "genres": []})

    # Ensure corrections list exists
    if "corrections" not in prefs:
        prefs["corrections"] = []

    prefs["corrections"].append(preference)

    # Cap at MAX_CORRECTIONS to prevent unbounded growth (oldest roll off)
    prefs["corrections"] = prefs["corrections"][-MAX_CORRECTIONS:]

    tool_context.state["user_prefs"] = prefs
    logger.info("Saved user preference: %s", preference)

    return {
        "status": "success",
        "message": f"Noted: {preference}",
        "total_preferences": len(prefs["corrections"]),
    }


async def get_user_profile(tool_context: ToolContext) -> dict:
    """Retrieves the stored preferences and corrections for this user.
    Use this when the user asks what you remember about them or their preferences.

    Returns:
        A dictionary containing the user's stored preferences.
    """
    prefs = tool_context.state.get("user_prefs", {})
    return {
        "status": "success",
        "user_prefs": prefs,
        "corrections_count": len(prefs.get("corrections", [])),
    }


async def clear_preferences(tool_context: ToolContext) -> dict:
    """Clears all stored preferences and corrections for this user.
    Use this when the user asks to forget their preferences or start fresh.

    Returns:
        A confirmation that preferences were cleared.
    """
    tool_context.state["user_prefs"] = {"corrections": [], "genres": []}
    logger.info("Cleared all user preferences")

    return {
        "status": "success",
        "message": "All preferences cleared. Starting fresh.",
    }
