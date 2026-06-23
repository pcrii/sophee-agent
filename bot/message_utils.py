"""Message processing utilities for the Sophee Discord bot."""

import base64
import logging
import re

import discord
from google.genai import types

logger = logging.getLogger("sophee.bot.messages")


def bracket_urls(text: str) -> str:
    """Wraps URLs in <> brackets to suppress Discord's auto-embed previews."""
    if not text:
        return text
    # Handles Wikipedia URLs with parentheses like https://en.wikipedia.org/wiki/Foo_(bar)
    pattern = r'(?<!<)(https?://[^\s()<>"]+(?:\([^\s()<>"]+\))?)(?!>)'
    return re.sub(pattern, r'<\1>', text)


async def send_message_in_chunks(message, text, reference=None, is_thread=False):
    """Splits long messages into <=1950 char chunks, preserving code blocks."""
    text = bracket_urls(text)
    max_length = 1950
    chunks = []
    current_chunk = ""
    in_code_block = False

    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block

        if len(current_chunk) + len(line) <= max_length:
            current_chunk += line
        else:
            if in_code_block:
                current_chunk += "```\n"
                chunks.append(current_chunk)
                current_chunk = "```\n" + line
            else:
                chunks.append(current_chunk)
                current_chunk = line

    if current_chunk:
        chunks.append(current_chunk)

    for i, chunk in enumerate(chunks):
        try:
            if is_thread:
                await message.send(chunk)
            elif i == 0 and hasattr(message, "reply"):
                await message.reply(chunk)
            else:
                if hasattr(message, "send"):
                    await message.send(chunk)
                else:
                    await message.channel.send(chunk)
        except discord.errors.HTTPException as e:
            logger.error("Error sending message chunk: %s", e)


async def fetch_chunked_context(replied_message, time_threshold_seconds=5.0, max_chunks=5) -> str:
    """Collects adjacent messages by the same author sent within a small time window to reconstruct chunked posts."""
    author = replied_message.author
    channel = replied_message.channel

    chunks = [(replied_message.id, replied_message.created_at, replied_message.content)]

    # Fetch messages before the replied message (older)
    try:
        async for msg in channel.history(limit=max_chunks, before=replied_message):
            if msg.author == author:
                prev_time = chunks[-1][1]
                time_diff = abs((prev_time - msg.created_at).total_seconds())
                if time_diff <= time_threshold_seconds:
                    chunks.append((msg.id, msg.created_at, msg.content))
                else:
                    break
    except Exception as e:
        logger.error("Error fetching history before replied message: %s", e)

    # Fetch messages after the replied message (newer)
    chunks_after = []
    try:
        async for msg in channel.history(limit=max_chunks, after=replied_message):
            if msg.author == author:
                ref_time = chunks_after[-1][1] if chunks_after else replied_message.created_at
                time_diff = abs((msg.created_at - ref_time).total_seconds())
                if time_diff <= time_threshold_seconds:
                    chunks_after.append((msg.id, msg.created_at, msg.content))
                else:
                    break
    except Exception as e:
        logger.error("Error fetching history after replied message: %s", e)

    # Combine chronologically
    all_chunks = list(reversed(chunks)) + chunks_after
    return "\n".join([c[2] for c in all_chunks if c[2]])


async def read_image_attachment(attachment) -> dict | None:
    """Reads a Discord image attachment and returns base64-encoded data with mime type."""
    if not (attachment.content_type and attachment.content_type.startswith("image/")):
        return None
    try:
        img_bytes = await attachment.read()
        logger.info("Loaded image attachment: %s (%d bytes)", attachment.filename, len(img_bytes))
        return {
            "data": base64.b64encode(img_bytes).decode("utf-8"),
            "mime_type": attachment.content_type,
            "raw_bytes": img_bytes,
        }
    except Exception as e:
        logger.error("Error reading attachment %s: %s", attachment.filename, e)
        return None
