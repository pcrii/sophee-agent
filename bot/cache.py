"""Image metadata cache for tracking generated image prompts, styles, and sessions."""

import asyncio
import json
import logging
import os

logger = logging.getLogger("sophee.bot.cache")

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bot_image_cache.json"
)
CACHE_LIMIT = 1000

_lock = asyncio.Lock()


def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Error loading image metadata cache: %s", e)
        return {}


def _save_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Error saving image metadata cache: %s", e)


async def save_image_metadata(
    message_id: str,
    prompt: str,
    style: dict | None,
    resolution: str,
    session_id: str,
):
    """Saves metadata for a generated image, keyed by the Discord Message ID.
    Enforces a maximum cache size of CACHE_LIMIT to prevent disk bloat.
    """
    if not message_id:
        return

    async with _lock:
        cache = _load_cache()

        # Enforce limit by popping the oldest key if we exceed CACHE_LIMIT
        if len(cache) >= CACHE_LIMIT:
            oldest_key = next(iter(cache))
            cache.pop(oldest_key, None)

        cache[str(message_id)] = {
            "prompt": prompt,
            "style": style,
            "resolution": resolution,
            "session_id": session_id,
        }
        _save_cache(cache)
        logger.debug("Cached image metadata for message %s", message_id)


async def get_image_metadata(message_id: str) -> dict | None:
    """Retrieves the metadata dictionary for the given Discord Message ID."""
    if not message_id:
        return None

    async with _lock:
        cache = _load_cache()
        return cache.get(str(message_id))
