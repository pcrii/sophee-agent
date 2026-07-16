"""Shared artifact helpers used by bot views and client.py.

Centralises the logic for saving/loading the persistent reference image artifact
so it can be called from:
  - ProcessedImageView.use_as_ref_callback (button click)
  - on_interaction fallback handler (post-restart button click)
  - execute_agent_turn (auto-restore into session state before agent runs)
"""

import base64
import logging

import aiohttp
import discord
from google.genai import types

logger = logging.getLogger("sophee.bot.artifact_helpers")

# Artifact filename for the user's persistent reference image.
# "user:" prefix makes it user-scoped in ADK FileArtifactService (not session-scoped).
REFERENCE_IMAGE_KEY = "reference_image_latest.png"
APP_NAME = "app"


def _get_services():
    """Lazy import to avoid circular imports — client.py is the source of truth."""
    from bot.client import artifact_service, session_service, update_session_state
    return artifact_service, session_service, update_session_state


async def _download_first_image_attachment(message: discord.Message) -> tuple[bytes, str] | tuple[None, None]:
    """Download the first image attachment from a Discord message."""
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            async with aiohttp.ClientSession() as http:
                async with http.get(attachment.url) as resp:
                    if resp.status == 200:
                        return await resp.read(), attachment.content_type
    return None, None


async def save_reference_image_from_message(
    message: discord.Message,
    user_id: str,
    session_id: str,
) -> None:
    """Download the image from a Discord message and save it as the user's reference artifact.

    Also injects it into session state immediately so the agent can use it in the
    next turn without waiting for an explicit restore.

    Raises:
        ValueError: if no image attachment found on the message.
        RuntimeError: if the artifact save fails.
    """
    artifact_service, session_service, update_session_state = _get_services()

    img_bytes, mime_type = await _download_first_image_attachment(message)
    if not img_bytes:
        raise ValueError("No image attachment found on that message.")

    mime_type = mime_type or "image/png"
    # Normalise to PNG-friendly mime
    if "jpeg" in mime_type or "jpg" in mime_type:
        ext_mime = "image/jpeg"
    else:
        ext_mime = "image/png"

    part = types.Part(inline_data=types.Blob(data=img_bytes, mime_type=ext_mime))

    # Save to FileArtifactService — writes to data/artifacts/ on disk, survives restarts
    try:
        await artifact_service.save_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            filename=REFERENCE_IMAGE_KEY,
            artifact=part,
        )
        logger.info("Saved reference image artifact for user=%s session=%s", user_id, session_id)
    except Exception as e:
        raise RuntimeError(f"Artifact save failed: {e}") from e

    # Set artifact reference in session state (not base64 — keeps state lightweight)
    await update_session_state(user_id, session_id, {
        "latest_input_image_artifact": REFERENCE_IMAGE_KEY,
        "latest_input_image_mime": ext_mime,
    })


async def restore_reference_image_to_session(user_id: str, session_id: str) -> bool:
    """Load reference image artifact into session state if session has no active reference.

    Called at the start of execute_agent_turn so the agent always has the reference
    image available even after a 4-hour session event wipe or bot restart.

    Returns:
        True if a reference image was restored, False if none existed or already set.
    """
    artifact_service, session_service, update_session_state = _get_services()

    try:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        # If session already has a reference image artifact set, skip
        if session and session.state.get("latest_input_image_artifact"):
            return False

        part = await artifact_service.load_artifact(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            filename=REFERENCE_IMAGE_KEY,
        )
        if not (part and part.inline_data and part.inline_data.data):
            return False

        mime = part.inline_data.mime_type or "image/png"
        await update_session_state(user_id, session_id, {
            "latest_input_image_artifact": REFERENCE_IMAGE_KEY,
            "latest_input_image_mime": mime,
        })
        logger.info("Restored reference image artifact into session state for user=%s", user_id)
        return True

    except Exception:
        # Artifact doesn't exist yet — that's normal
        return False
