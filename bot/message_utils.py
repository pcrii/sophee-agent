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


async def send_message_in_chunks(message, text, reference=None, is_thread=False, embed=None, view=None):
    """Splits long messages into <=1950 char chunks, preserving code blocks, and attaching embed/view on last chunk."""
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

    sent_messages = []
    for i, chunk in enumerate(chunks):
        try:
            current_embed = embed if i == len(chunks) - 1 else None
            current_view = view if i == len(chunks) - 1 else None
            sent_msg = None
            if is_thread:
                sent_msg = await message.send(chunk, embed=current_embed, view=current_view)
            elif i == 0 and hasattr(message, "reply"):
                sent_msg = await message.reply(chunk, embed=current_embed, view=current_view)
            else:
                if hasattr(message, "send"):
                    sent_msg = await message.send(chunk, embed=current_embed, view=current_view)
                else:
                    sent_msg = await message.channel.send(chunk, embed=current_embed, view=current_view)
            if sent_msg:
                sent_messages.append(sent_msg)
        except Exception as e:
            logger.error("Error sending chunk: %s", e)

    return sent_messages






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
