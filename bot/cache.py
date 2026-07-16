"""Image metadata cache for tracking generated image prompts, styles, and sessions."""

import asyncio
import json
import logging
import os
import time

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
    session_id: str = None,
    image_artifact: str = None,
    parent_image_artifact: str = None,
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
            "image_artifact": image_artifact,
            "parent_image_artifact": parent_image_artifact,
            "timestamp": time.time(),
        }
        _save_cache(cache)
        logger.debug("Cached image metadata for message %s (artifact: %s, parent: %s)", message_id, image_artifact, parent_image_artifact)


async def get_image_metadata(message_id: str) -> dict | None:
    """Retrieves the metadata dictionary for the given Discord Message ID."""
    if not message_id:
        return None

    async with _lock:
        cache = _load_cache()
        return cache.get(str(message_id))


async def cleanup_image_metadata():
    """Removes image metadata older than 24 hours (86400 seconds)."""
    async with _lock:
        cache = _load_cache()
        now = time.time()
        expired_count = 0
        cleaned_cache = {}
        for msg_id, data in cache.items():
            ts = data.get("timestamp")
            if ts is None:
                # Keep legacy entries but assign current timestamp so they expire in 24h
                data["timestamp"] = now
                cleaned_cache[msg_id] = data
            elif now - ts <= 86400:
                cleaned_cache[msg_id] = data
            else:
                expired_count += 1

        if expired_count > 0:
            _save_cache(cleaned_cache)
            logger.info("Cleaned up %d expired cache entries", expired_count)
        else:
            logger.debug("No expired image metadata entries to purge")


async def save_text_metadata(
    message_id: str,
    agent_name: str,
    config: dict,
):
    """Saves metadata for a generated text response, keyed by the Discord Message ID."""
    if not message_id:
        return

    async with _lock:
        cache = _load_cache()

        if len(cache) >= CACHE_LIMIT:
            oldest_key = next(iter(cache))
            cache.pop(oldest_key, None)

        cache[str(message_id)] = {
            "type": "text",
            "agent_name": agent_name,
            "config": config,
            "timestamp": time.time(),
        }
        _save_cache(cache)


async def get_text_metadata(message_id: str) -> dict | None:
    """Retrieves the text metadata dictionary for the given Discord Message ID."""
    if not message_id:
        return None

    async with _lock:
        cache = _load_cache()
        data = cache.get(str(message_id))
        if data and data.get("type") == "text":
            return data
        return None
