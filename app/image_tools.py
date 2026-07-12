"""Image generation, preprocessing, and settings tools for Sophee ADK agents.

Migrated from tools.py. Contains:
- generate_image: text-to-image and image editing via Interactions API
- preprocess_image: Canny edge, pencil sketch, posterize, blur for canvas prep
- show_image_settings: flags the bot to display the settings embed
- set_image_defaults: sets persistent session defaults
"""

import base64
import hashlib
import logging
import os
import time

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

logger = logging.getLogger("sophee.app.image_tools")


# ---------------------------------------------------------------------------
# Generate Image
# ---------------------------------------------------------------------------

async def generate_image(
    prompt: str,
    tool_context: ToolContext,
    resolution: str = None,
    aspect_ratio: str = None,
    model: str = None,
    edit_mode: str = "edit",
    seed: int = None,
    temperature: float = None,
    enable_search_grounding: bool = False,
) -> dict:
    """Generates OR edits a high-quality image based on a detailed text prompt.
    If the user uploads an image or requests an edit, ALWAYS use this tool.
    The tool automatically uses their latest uploaded/generated image as the reference.
    Saves the output image to the user's artifacts (persistent across sessions).

    Args:
        prompt: The detailed description of the image to generate or the edit to perform.
        resolution: Optional resolution override ('0.5k', '1k', '2k', '4k'). Do NOT pass unless explicitly requested.
        aspect_ratio: Optional aspect ratio override (e.g. '1:1', '16:9', '9:16'). Intelligently choose based on subject if not specified.
        model: Optional model override. Do NOT pass unless explicitly requested.
        edit_mode: How to use a reference image if one exists:
            - 'edit' (default): The reference IS the canvas. Modify only what the prompt specifies, preserve everything else exactly.
            - 'reimagine': Use the reference's structure and subject as a guide, but freely reinterpret style, color, and atmosphere.
            - 'reference': The reference is pure inspiration/vibe only. Ignore its dimensions and details entirely. Generate fresh using the reference as a mood board.
        seed: Optional integer seed for reproducibility. Same seed + same prompt = similar results.
        temperature: Optional creativity dial (0.0 = very literal, 1.0 = very creative). Uses model default if not set.
        enable_search_grounding: If true, enables Grounding with Google Search. Forces flash model.

    Returns:
        A dictionary containing the generated image's artifact name.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    try:
        tool_context.state["last_generated_prompt"] = prompt

        # Honour start-fresh flag (set by bot when user explicitly wants a blank slate)
        start_fresh = tool_context.state.get("start_fresh_image", False)
        if start_fresh:
            tool_context.state["start_fresh_image"] = False
            tool_context.state.pop("latest_input_image", None)

        # Check for a cached reference image in session state
        latest_img = tool_context.state.get("latest_input_image")
        if latest_img:
            raw_bytes = base64.b64decode(latest_img["data"])

            if edit_mode == "edit":
                # Tight edit — strong preservation phrasing
                final_prompt = (
                    f"Using the provided image, perform the following edit: '{prompt}'. "
                    "Keep everything else in the image exactly the same, "
                    "preserving the original style, lighting, composition, and all details."
                )
            elif edit_mode == "reimagine":
                # Structural reinterpretation — use composition/subject, free on style
                final_prompt = (
                    f"Using the composition and subject of the provided image as a structural guide, "
                    f"reinterpret it as follows: {prompt}. "
                    "You may freely change style, color palette, atmosphere, and rendering technique."
                )
            else:
                # reference — vibe only, treat like a mood board
                final_prompt = (
                    f"Inspired by the mood, palette, and general atmosphere of the reference image, "
                    f"generate a new image: {prompt}. "
                    "Do not replicate the reference directly — use it only as inspiration."
                )

            input_data = [
                {
                    "type": "image",
                    "data": base64.b64encode(raw_bytes).decode("utf-8"),
                    "mime_type": latest_img["mime_type"],
                },
                {"type": "text", "text": final_prompt},
            ]
            debug_info = {
                "prompt": final_prompt,
                "edit_mode": edit_mode,
                "has_image": True,
                "mime_type": latest_img["mime_type"],
                "image_bytes_length": len(raw_bytes),
                "api": "interactions",
            }
        else:
            final_prompt = prompt
            input_data = prompt
            debug_info = {
                "prompt": prompt,
                "has_image": False,
                "api": "interactions",
            }

        # --- Resolution ---
        effective_resolution = resolution or tool_context.state.get("default_image_resolution", "0.5k")

        # --- Aspect ratio ---
        # In 'reference' mode, ignore the reference image's AR and use session/user-specified default.
        # In 'edit' / 'reimagine', if no AR is specified let the API match the original image's AR.
        if aspect_ratio:
            effective_ratio = aspect_ratio
        elif edit_mode == "reference" or not latest_img:
            effective_ratio = tool_context.state.get("default_image_ratio", "1:1")
        else:
            # edit / reimagine: let API default to original image AR
            effective_ratio = None

        # --- Model ---
        effective_model = model or tool_context.state.get("default_image_model", "gemini-3.1-flash-lite-image")
        if enable_search_grounding:
            effective_model = "gemini-3.1-flash-image"
        if effective_model == "gemini-3.1-flash-lite-image":
            effective_resolution = "1k"  # Lite only supports 1K

        # --- Map resolution string to API size ---
        res_lower = effective_resolution.lower().strip()
        api_image_size = {"1k": "1K", "2k": "2K", "4k": "4K"}.get(res_lower, "512")

        tool_context.state["latest_resolution"] = effective_resolution

        # --- Seed / temperature (session defaults as fallback) ---
        effective_seed = seed if seed is not None else tool_context.state.get("default_image_seed")
        effective_temperature = (
            temperature if temperature is not None else tool_context.state.get("default_image_temperature")
        )

        # --- Build response_format ---
        response_format: dict = {"type": "image", "image_size": api_image_size}
        if effective_ratio:
            response_format["aspect_ratio"] = effective_ratio

        # --- Build kwargs ---
        kwargs: dict = {
            "model": effective_model,
            "input": input_data,
            "response_format": response_format,
        }

        # Wire generation_config for seed / temperature
        generation_config: dict = {}
        if effective_seed is not None:
            generation_config["seed"] = effective_seed
        if effective_temperature is not None:
            generation_config["temperature"] = effective_temperature
        if generation_config:
            kwargs["generation_config"] = generation_config

        if enable_search_grounding:
            kwargs["tools"] = [{"type": "google_search", "search_types": ["web_search", "image_search"]}]
            debug_info["grounding_enabled"] = True

        # Save settings for embed display
        tool_context.state["last_image_settings"] = {
            "model": effective_model,
            "resolution": effective_resolution,
            "aspect_ratio": effective_ratio,
            "grounding_enabled": enable_search_grounding,
            "has_image": debug_info.get("has_image", False),
            "edit_mode": edit_mode,
            "seed": effective_seed,
            "temperature": effective_temperature,
            "prompt": final_prompt,
        }

        import json

        debug_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "last_image_payload.json",
        )
        try:
            with open(debug_path, "w") as f:
                json.dump(debug_info, f)
        except Exception:
            pass

        try:
            interaction = await client.aio.interactions.create(**kwargs)

            debug_out_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data",
                "last_image_out.json",
            )
            try:
                with open(debug_out_path, "w") as f:
                    f.write(str(interaction))
            except Exception:
                pass

            generated_image = interaction.output_image
            image_bytes = None
            if generated_image:
                image_bytes = base64.b64decode(generated_image.data)

        except Exception as e:
            logger.error("Error generating image via Interactions API: %s", e)
            return {"status": "error", "message": f"Error generating image: {e}"}

        # Clear input-side state
        tool_context.state["rolled_style"] = None
        tool_context.state["latest_input_image"] = None
        tool_context.state["latest_input_image_artifact"] = None

        if image_bytes:
            logger.warning("Image bytes retrieved! Length: %d", len(image_bytes))
            part = types.Part(
                inline_data=types.Blob(data=image_bytes, mime_type="image/jpeg")
            )
            artifact_name = (
                f"user:generated_image_{hashlib.md5(prompt.encode()).hexdigest()[:8]}_{int(time.time())}.jpeg"
            )
            logger.info("Saving artifact: %s", artifact_name)
            try:
                await tool_context.save_artifact(artifact_name, part)
                logger.info("Successfully saved artifact: %s", artifact_name)
            except Exception as save_err:
                logger.error("Failed to save artifact %s: %s", artifact_name, save_err)

            return {
                "status": "success",
                "artifact_name": artifact_name,
                "message": "Image successfully generated and saved.",
            }
        else:
            logger.warning(
                "API returned success but no image data. output_image: %s", generated_image
            )
            return {"status": "error", "message": "No image generated."}

    except Exception as e:
        logger.exception("FATAL Error generating image:")
        return {"status": "error", "message": f"Error generating image: {e}"}


# ---------------------------------------------------------------------------
# Preprocess Image
# ---------------------------------------------------------------------------

async def preprocess_image(
    tool_context: ToolContext,
    mode: str = "canny",
) -> dict:
    """Applies a visual preprocessing transform to the current reference image.
    This replaces the reference image in session state with the processed version,
    so it can be used as a cleaner canvas for the next generate_image call.

    Use this when the user wants to:
    - Strip away texture/detail before reimagining ('trace this', 'outline this', 'use the structure of')
    - Create an abstract version before a style transfer
    - Chain preprocessing with generation: call this first, then call generate_image

    Args:
        mode: The preprocessing transform to apply:
            - 'canny': Extract edge outlines only (black lines on white). Best for structure-preserving generation.
            - 'sketch': Pencil sketch effect. Removes texture, keeps line work.
            - 'posterize': Reduce color complexity. Keeps shapes, removes noise and fine detail.
            - 'blur': Heavy Gaussian blur. Very abstract canvas — loose vibe reference.

    Returns:
        A dict with status and the artifact name of the preprocessed image (shown to user as a preview).
    """
    latest_img = tool_context.state.get("latest_input_image")
    if not latest_img:
        return {
            "status": "error",
            "message": "No reference image found in session. Please upload or generate an image first.",
        }

    try:
        from PIL import Image, ImageFilter, ImageOps
        import io
        import numpy as np
    except ImportError as e:
        return {"status": "error", "message": f"Missing image processing dependency: {e}"}

    raw_bytes = base64.b64decode(latest_img["data"])
    pil_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    mode = mode.lower().strip()

    try:
        if mode == "canny":
            try:
                import cv2
            except ImportError:
                return {
                    "status": "error",
                    "message": "opencv-python-headless is not installed. Run: uv add opencv-python-headless",
                }
            arr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(blurred, 50, 150)
            # Invert: white background, black edges (friendlier canvas for the image model)
            edges_inv = cv2.bitwise_not(edges)
            processed = Image.fromarray(edges_inv).convert("RGB")

        elif mode == "sketch":
            # Pencil sketch: grayscale -> invert -> blur -> dodge blend
            import numpy as np

            gray = pil_img.convert("L")
            inverted = ImageOps.invert(gray)
            blurred = inverted.filter(ImageFilter.GaussianBlur(radius=10))
            gray_arr = np.array(gray, dtype=np.float32)
            blur_arr = np.array(blurred, dtype=np.float32)
            sketch_arr = np.clip(
                (gray_arr * 255.0) / (255.0 - blur_arr + 1e-6), 0, 255
            ).astype(np.uint8)
            processed = Image.fromarray(sketch_arr).convert("RGB")

        elif mode == "posterize":
            processed = ImageOps.posterize(pil_img, bits=3)

        elif mode == "blur":
            processed = pil_img.filter(ImageFilter.GaussianBlur(radius=12))

        else:
            return {
                "status": "error",
                "message": f"Unknown mode '{mode}'. Valid options: canny, sketch, posterize, blur.",
            }

    except Exception as e:
        logger.error("Preprocessing error (mode=%s): %s", mode, e)
        return {"status": "error", "message": f"Preprocessing failed: {e}"}

    # Encode back to JPEG bytes
    import io

    buf = io.BytesIO()
    processed.save(buf, format="JPEG", quality=90)
    processed_bytes = buf.getvalue()
    processed_b64 = base64.b64encode(processed_bytes).decode("utf-8")

    # Replace the session reference image with the processed version
    tool_context.state["latest_input_image"] = {
        "data": processed_b64,
        "mime_type": "image/jpeg",
    }

    # Save as artifact so Discord shows the preprocessing result as a preview
    part = types.Part(
        inline_data=types.Blob(data=processed_bytes, mime_type="image/jpeg")
    )
    artifact_name = f"user:preprocessed_{mode}_{int(time.time())}.jpeg"
    try:
        await tool_context.save_artifact(artifact_name, part)
    except Exception as save_err:
        logger.error("Failed to save preprocessed artifact: %s", save_err)

    return {
        "status": "success",
        "artifact_name": artifact_name,
        "message": (
            f"Image preprocessed with '{mode}' mode and set as the active reference. "
            "Call generate_image to continue."
        ),
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Show / Set Image Settings
# ---------------------------------------------------------------------------

async def show_image_settings(tool_context: ToolContext) -> dict:
    """Displays the user's current image generation settings as a Discord embed.
    Call this when the user asks to see their current image settings or defaults.
    """
    tool_context.state["show_image_settings_embed"] = True
    return {"status": "success", "message": "Image settings embed staged for display."}


async def set_image_defaults(
    tool_context: ToolContext,
    model: str = None,
    resolution: str = None,
    aspect_ratio: str = None,
    seed: int = None,
    temperature: float = None,
) -> dict:
    """Sets persistent default values for image generation in the current session.
    These defaults are automatically applied to subsequent generate_image calls.

    Args:
        model: Model code ('gemini-3.1-flash-lite-image', 'gemini-3.1-flash-image', 'gemini-3-pro-image').
        resolution: Resolution ('0.5k', '1k', '2k', '4k').
        aspect_ratio: Aspect ratio (e.g. '1:1', '16:9', '9:16').
        seed: Integer seed for reproducibility. Pass -1 to clear the current seed.
        temperature: Creativity dial (0.0-1.0). Pass -1 to clear and use model default.
    """
    if model:
        tool_context.state["default_image_model"] = model
    if resolution:
        tool_context.state["default_image_resolution"] = resolution
    if aspect_ratio:
        tool_context.state["default_image_ratio"] = aspect_ratio
    if seed is not None:
        if seed == -1:
            tool_context.state.pop("default_image_seed", None)
        else:
            tool_context.state["default_image_seed"] = seed
    if temperature is not None:
        if temperature == -1:
            tool_context.state.pop("default_image_temperature", None)
        else:
            tool_context.state["default_image_temperature"] = temperature

    return {"status": "success", "message": "Image defaults updated."}
