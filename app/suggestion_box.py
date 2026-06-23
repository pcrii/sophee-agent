"""Suggestion box scraper — reads messages from a designated Discord channel
and appends them to a persistent local file for later review.

The file acts as a rolling notebook: new entries are appended at the bottom,
and old entries are pruned from the top when the file exceeds MAX_LINES.
"""

import json
import logging
import os
from datetime import datetime, timezone

from google.adk.tools import ToolContext

logger = logging.getLogger("sophee.app.suggestion_box")

# Configuration
SUGGESTION_CHANNEL_ID = int(os.getenv("SUGGESTION_CHANNEL_ID", "0"))
SUGGESTION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "suggestion_box.md")
LAST_SCRAPED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".suggestion_box_cursor.json")
MAX_LINES = 500  # Prune oldest entries when file exceeds this


def _ensure_data_dir():
    """Creates the data directory if it doesn't exist."""
    data_dir = os.path.dirname(SUGGESTION_FILE)
    os.makedirs(data_dir, exist_ok=True)


def _get_last_scraped_id() -> int | None:
    """Returns the ID of the last scraped message, or None if never scraped."""
    if os.path.exists(LAST_SCRAPED_FILE):
        try:
            with open(LAST_SCRAPED_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_message_id")
        except Exception:
            pass
    return None


def _set_last_scraped_id(message_id: int):
    """Saves the ID of the most recently scraped message."""
    _ensure_data_dir()
    with open(LAST_SCRAPED_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_message_id": message_id, "scraped_at": datetime.now(timezone.utc).isoformat()}, f)


def _prune_file():
    """If the suggestion file exceeds MAX_LINES, trim oldest entries from the top."""
    if not os.path.exists(SUGGESTION_FILE):
        return

    with open(SUGGESTION_FILE, encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) > MAX_LINES:
        # Keep the last MAX_LINES lines
        trimmed = lines[-MAX_LINES:]
        with open(SUGGESTION_FILE, "w", encoding="utf-8") as f:
            f.writelines(trimmed)
        logger.info("Pruned suggestion box from %d to %d lines", len(lines), MAX_LINES)


async def scrape_suggestion_box(tool_context: ToolContext) -> dict:
    """Scrapes new messages from the suggestion box Discord channel and appends
    them to the local suggestion_box.md file. Only fetches messages newer than
    the last scrape. Use this when the user asks to scrape, check, or pull
    their suggestion box / notes / ideas channel.

    Returns:
        A dictionary with the number of new messages scraped and a preview.
    """
    if not SUGGESTION_CHANNEL_ID:
        return {
            "status": "error",
            "message": "SUGGESTION_CHANNEL_ID is not set in .env — add it to enable this feature.",
        }

    from app.radio_state import get_discord_client, resolve_guild_id

    client = get_discord_client()
    if not client:
        return {"status": "error", "message": "Discord client not available."}

    channel = client.get_channel(SUGGESTION_CHANNEL_ID)
    if not channel:
        try:
            channel = await client.fetch_channel(SUGGESTION_CHANNEL_ID)
        except Exception as e:
            logger.error("Error fetching suggestion channel %s: %s", SUGGESTION_CHANNEL_ID, e)
            return {
                "status": "error",
                "message": f"Could not find suggestion channel {SUGGESTION_CHANNEL_ID}. Make sure the bot has access and the ID is correct.",
            }

    # Guild isolation — only allow scraping from the same guild
    suggestion_guild_id = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        requesting_channel_id = int(channel_id_str)
    except ValueError:
        requesting_channel_id = 0
    requesting_guild_id = resolve_guild_id(requesting_channel_id)

    if suggestion_guild_id and requesting_guild_id and suggestion_guild_id != requesting_guild_id:
        return {
            "status": "error",
            "message": "The suggestion box is not available in this server.",
        }

    # Fetch messages after the last scraped ID
    last_id = _get_last_scraped_id()
    kwargs = {"limit": 200, "oldest_first": True}
    if last_id:
        import discord
        kwargs["after"] = discord.Object(id=last_id)

    try:
        messages = []
        async for msg in channel.history(**kwargs):
            # Skip bot messages — we only want user notes
            if msg.author.bot:
                continue
            messages.append(msg)
    except Exception as e:
        logger.error("Error fetching messages from suggestion channel: %s", e)
        return {"status": "error", "message": f"Error reading channel: {e}"}

    if not messages:
        return {
            "status": "success",
            "new_messages": 0,
            "message": "No new messages in the suggestion box since last scrape.",
        }

    # Format and append to file
    _ensure_data_dir()
    new_entries = []
    for msg in messages:
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
        author = msg.author.display_name
        content = msg.content.strip()

        if not content and msg.attachments:
            content = "[attachment: " + ", ".join(a.filename for a in msg.attachments) + "]"
        if not content:
            continue

        entry = f"- [ ] **[{timestamp}]** {author}: {content}"
        new_entries.append(entry)

    if not new_entries:
        return {
            "status": "success",
            "new_messages": 0,
            "message": "No new text messages found (only bot messages or empty messages).",
        }

    # Append to file
    with open(SUGGESTION_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## Scraped {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        for entry in new_entries:
            f.write(entry + "\n")
        f.write("\n")

    # Update cursor
    _set_last_scraped_id(messages[-1].id)

    # Prune if too long
    _prune_file()

    # Build preview (last 5 entries)
    preview = new_entries[-5:] if len(new_entries) > 5 else new_entries

    return {
        "status": "success",
        "new_messages": len(new_entries),
        "file_path": SUGGESTION_FILE,
        "message": f"Scraped {len(new_entries)} new message(s) from the suggestion box.",
        "preview": preview,
    }


async def read_suggestion_box(tool_context: ToolContext) -> dict:
    """Reads and returns the current contents of the suggestion box file.
    Use this when the user wants to review, discuss, or go through their
    saved notes/ideas/suggestions.

    Returns:
        A dictionary with the file contents.
    """
    if not os.path.exists(SUGGESTION_FILE):
        return {
            "status": "info",
            "message": "The suggestion box is empty. No notes have been scraped yet.",
            "contents": "",
        }

    with open(SUGGESTION_FILE, encoding="utf-8") as f:
        contents = f.read()

    if not contents.strip():
        return {
            "status": "info",
            "message": "The suggestion box file exists but is empty.",
            "contents": "",
        }

    # Count entries
    entry_count = contents.count("\n- **[")

    return {
        "status": "success",
        "entry_count": entry_count,
        "contents": contents,
        "file_path": SUGGESTION_FILE,
    }
