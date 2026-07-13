"""Adventure tools for the tabletop RPG Dungeon Master (DM) engine."""

import logging
import discord
from google.adk.tools import ToolContext
from app.db import session_service

logger = logging.getLogger("sophee.app.adventure_tools")


async def start_adventure(genre: str, character_concept: str = "", *, tool_context: ToolContext) -> dict:
    """Starts a new tabletop RPG adventure. Creates a dedicated thread for the session.

    Args:
        genre: The genre of the adventure (e.g. 'cyberpunk', 'dark fantasy', 'solarpunk').
        character_concept: The player's initial character description (e.g. 'decker named Jax').

    Returns:
        A dictionary containing the status of the thread creation.
    """
    session = tool_context.session
    if not session:
        return {"status": "error", "message": "No active session context."}

    session_id = session.id
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        return {"status": "error", "message": f"Invalid session ID format: {session_id}"}

    user_id = session.user_id
    from app.radio_state import get_discord_client, resolve_guild_id
    client = get_discord_client()
    if not client:
        return {"status": "error", "message": "Discord client not registered."}

    channel = client.get_channel(channel_id)
    if not channel:
        return {"status": "error", "message": f"Channel {channel_id} not found."}

    is_thread = isinstance(channel, discord.Thread)
    if is_thread:
        thread = channel
    else:
        # Create a new public thread on the channel
        try:
            thread_name = f"📖 {genre.title()} Adventure"
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=60
            )
        except Exception as e:
            logger.error("Failed to create thread: %s", e)
            return {"status": "error", "message": f"Could not create thread: {e}"}

    # Initialize state for the thread's session ID
    thread_session_id = f"discord_{thread.id}"


    thread_session = await session_service.get_session(
        app_name="app", user_id=user_id, session_id=thread_session_id
    )
    if not thread_session:
        thread_session = await session_service.create_session(
            app_name="app", user_id=user_id, session_id=thread_session_id
        )

    # Initialize state variables
    thread_session.state["adventure_active"] = True
    thread_session.state["genre"] = genre
    thread_session.state["character_concept"] = character_concept or "A mysterious traveler"
    thread_session.state["player_health"] = "100/100"
    thread_session.state["inventory"] = []
    thread_session.state["quest_log"] = []
    thread_session.state["tension"] = 10
    thread_session.state["choices"] = ["Say 'start' to begin"]

    # Save state to database
    from google.adk.events import Event, EventActions
    import time
    import uuid

    dummy_event = Event(
        timestamp=time.time(),
        author="system",
        invocation_id=f"adv_init_{uuid.uuid4().hex[:8]}",
        actions=EventActions(state_delta=thread_session.state),
    )
    await session_service.append_event(thread_session, dummy_event)

    # Post welcome message in thread if it's new
    if not is_thread:
        try:
            await thread.send(
                f"**Greetings, traveler.** You have entered the realm of the Dungeon Master.\n"
                f"*Genre:* **{genre.title()}**\n"
                f"*Character:* **{character_concept or 'Not yet established'}**\n\n"
                f"Please describe your hero's ambitions and background to begin, or say **start** to dive straight in!"
            )
        except Exception as e:
            logger.warning("Failed to send welcome message to thread: %s", e)

    return {
        "status": "success",
        "message": f"Started adventure in thread {thread.name} (ID: {thread.id})",
        "thread_id": thread.id,
        "is_new_thread": not is_thread,
    }


async def update_adventure_state(
    *,
    health: str = None,
    add_inventory: str = None,
    remove_inventory: str = None,
    add_quest: str = None,
    complete_quest: str = None,
    choices: list[str] = None,
    tension_change: int = None,
    tool_context: ToolContext,
) -> dict:
    """Updates the active adventure's stats, items, quests, choices, and tension level.

    Args:
        health: The updated player health (e.g. '80/100', '45/50').
        add_inventory: A comma-separated string of items to add to inventory.
        remove_inventory: A comma-separated string of items to remove from inventory.
        add_quest: A new active quest description to add.
        complete_quest: An active quest description to complete (remove).
        choices: Up to 5 choice options to display as buttons for the next turn.
        tension_change: Positive or negative integer to adjust tension (range 0-100).
    """
    session = tool_context.session
    if not session or not session.state.get("adventure_active"):
        return {"status": "error", "message": "No active adventure in this session."}

    updates = {}

    # Update health
    if health is not None:
        session.state["player_health"] = health
        updates["player_health"] = health

    # Update inventory
    inventory = list(session.state.get("inventory", []))
    if add_inventory:
        items = [i.strip() for i in add_inventory.split(",") if i.strip()]
        for item in items:
            if item not in inventory:
                inventory.append(item)
        session.state["inventory"] = inventory
        updates["inventory"] = inventory
    if remove_inventory:
        items = [i.strip() for i in remove_inventory.split(",") if i.strip()]
        for item in items:
            if item in inventory:
                inventory.remove(item)
        session.state["inventory"] = inventory
        updates["inventory"] = inventory

    # Update quests
    quest_log = list(session.state.get("quest_log", []))
    if add_quest:
        if add_quest not in quest_log:
            quest_log.append(add_quest)
        session.state["quest_log"] = quest_log
        updates["quest_log"] = quest_log
    if complete_quest:
        if complete_quest in quest_log:
            quest_log.remove(complete_quest)
        session.state["quest_log"] = quest_log
        updates["quest_log"] = quest_log

    # Update choices (max 5)
    if choices is not None:
        choices = choices[:5]
        session.state["choices"] = choices
        updates["choices"] = choices

    # Update tension
    if tension_change is not None:
        current_tension = session.state.get("tension", 10)
        new_tension = max(0, min(100, current_tension + tension_change))
        session.state["tension"] = new_tension
        updates["tension"] = new_tension

    # Save to database

    from google.adk.events import Event, EventActions
    import time
    import uuid

    dummy_event = Event(
        timestamp=time.time(),
        author="system",
        invocation_id=f"adv_update_{uuid.uuid4().hex[:8]}",
        actions=EventActions(state_delta=updates),
    )
    await session_service.append_event(session, dummy_event)

    return {
        "status": "success",
        "message": "Adventure state updated.",
        "current_state": {
            "player_health": session.state.get("player_health"),
            "inventory": session.state.get("inventory"),
            "quest_log": session.state.get("quest_log"),
            "choices": session.state.get("choices"),
            "tension": session.state.get("tension"),
        }
    }


async def end_adventure(*, tool_context: ToolContext) -> dict:
    """Ends the active adventure in this session and archives the thread."""
    session = tool_context.session
    if not session or not session.state.get("adventure_active"):
        return {"status": "error", "message": "No active adventure to end."}

    session.state["adventure_active"] = False

    # Save updated state to database

    from google.adk.events import Event, EventActions
    import time
    import uuid

    dummy_event = Event(
        timestamp=time.time(),
        author="system",
        invocation_id=f"adv_end_{uuid.uuid4().hex[:8]}",
        actions=EventActions(state_delta={"adventure_active": False}),
    )
    await session_service.append_event(session, dummy_event)

    # Archive the thread
    session_id = session.id
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = None

    if channel_id:
        from app.radio_state import get_discord_client
        client = get_discord_client()
        if client:
            channel = client.get_channel(channel_id)
            if channel and isinstance(channel, discord.Thread):
                try:
                    await channel.edit(archived=True, locked=True)
                except Exception as e:
                    logger.warning("Failed to archive thread: %s", e)

    return {
        "status": "success",
        "message": "Adventure has been ended and the thread has been archived."
    }
