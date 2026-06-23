"""User preference and learning tools.

These tools allow the bot to remember per-user behavioral corrections
and preferences. Stored in session state and persisted via DatabaseSessionService.
"""

import logging
import os
from google.adk.tools import ToolContext

logger = logging.getLogger("sophee.app.user_tools")

# Maximum number of corrections to store per user
MAX_CORRECTIONS = 20


def _save_personalization_file(user_id: str, corrections: list):
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(project_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        file_path = os.path.join(data_dir, f"user_profile_{user_id}.txt")
        
        if corrections:
            prefs_str = "\n".join(f"- {c}" for c in corrections)
            pref_context = f"\n\n[USER PREFERENCES for this user:\n{prefs_str}]"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(pref_context)
            logger.info("Saved user personalization file: %s", file_path)
        else:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info("Removed empty user personalization file: %s", file_path)
    except Exception as e:
        logger.error("Error saving user personalization file: %s", e)


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
    tool_context.state["show_user_profile_embed"] = True
    
    _save_personalization_file(tool_context.user_id, prefs["corrections"])
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
    tool_context.state["show_user_profile_embed"] = True
    return {
        "status": "success",
        "user_prefs": prefs,
        "corrections_count": len(prefs.get("corrections", [])),
    }


async def delete_preference(index: int, tool_context: ToolContext) -> dict:
    """Deletes/removes a specific user preference statement by its 1-based index (as displayed in the profile list).
    Use this when the user asks you to forget, remove, or delete a specific preference or statement from their profile.

    Args:
        index: The 1-based index of the preference to remove.

    Returns:
        A confirmation message.
    """
    prefs = tool_context.state.get("user_prefs", {})
    corrections = prefs.get("corrections", [])
    if not corrections:
        return {"status": "error", "message": "No preferences recorded to delete."}
        
    if index < 1 or index > len(corrections):
        return {"status": "error", "message": f"Invalid index. Please specify a number between 1 and {len(corrections)}."}
        
    removed = corrections.pop(index - 1)
    prefs["corrections"] = corrections
    tool_context.state["user_prefs"] = prefs
    tool_context.state["show_user_profile_embed"] = True
    
    _save_personalization_file(tool_context.user_id, corrections)
    logger.info("Removed user preference: %s", removed)
    return {
        "status": "success",
        "message": f"Removed: '{removed}'",
        "total_preferences": len(corrections),
    }


async def clear_preferences(tool_context: ToolContext) -> dict:
    """Clears all stored preferences and corrections for this user.
    Use this when the user asks to forget their preferences or start fresh.

    Returns:
        A confirmation that preferences were cleared.
    """
    tool_context.state["user_prefs"] = {"corrections": [], "genres": []}
    tool_context.state["show_user_profile_embed"] = True
    _save_personalization_file(tool_context.user_id, [])
    logger.info("Cleared all user preferences")

    return {
        "status": "success",
        "message": "All preferences cleared. Starting fresh.",
    }
