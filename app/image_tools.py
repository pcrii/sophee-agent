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
from io import BytesIO
from typing import Any

from google import genai
from google.adk.tools import ToolContext
from google.genai import types
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger("sophee.app.image_tools")


# ---------------------------------------------------------------------------
# Generate Image
# ---------------------------------------------------------------------------

async def gemini_generate_image(
    prompt: str,
    tool_context: ToolContext,
    resolution: str = None,
    aspect_ratio: str = None,
    model: str = None,
    edit_mode: str = "reference",
    seed: int = None,
    temperature: float = None,
    enable_search_grounding: bool = False,
    postprocess_modes: list[str] = None,
) -> dict:
    """Generates OR edits a high-quality image based on a detailed text prompt.
    If the user uploads an image or requests an edit, ALWAYS use this tool.
    The tool automatically uses their latest uploaded/generated image as the reference.
    By default, it uses 'reference' mode. If the user explicitly asks to 'edit', 'modify', or 'change' the image, set edit_mode='edit'.
    If the user explicitly asks to 'reference' or 'use as inspiration', ensure edit_mode='reference'.
    Saves the output image to the user's artifacts (persistent across sessions).

    Args:
        prompt: The detailed description of the image to generate or the edit to perform.
        resolution: Optional resolution override ('0.5k', '1k', '2k', '4k'). Do NOT pass unless explicitly requested.
        aspect_ratio: Optional aspect ratio override (e.g. '1:1', '16:9', '9:16'). Intelligently choose based on subject if not specified.
        model: Optional model override. Do NOT pass unless explicitly requested.
        edit_mode: How to use a reference image if one exists:
            - 'reference' (default): The reference is pure inspiration/vibe only. Ignore its dimensions and details entirely. Generate fresh using the reference as a mood board.
            - 'reimagine': Use the reference's structure and subject as a guide, but freely reinterpret style, color, and atmosphere.
            - 'edit': The reference IS the canvas. Modify only what the prompt specifies, preserve everything else exactly.
        seed: Optional integer seed for reproducibility. Same seed + same prompt = similar results.
        temperature: Optional creativity dial (0.0 = very literal, 1.0 = very creative). Uses model default if not set.
        enable_search_grounding: If true, enables Grounding with Google Search. Forces flash model.
        postprocess_modes: Optional list of modes to run sequentially after generation (e.g. ["smart_crop", "remove_whitespace"]).

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

            # Apply any chained postprocessing modes
            applied_modes = []
            if postprocess_modes:
                for pm in postprocess_modes:
                    try:
                        processed_b = await preprocess_image_bytes(image_bytes, pm)
                        if processed_b:
                            image_bytes = processed_b
                            applied_modes.append(pm)
                            logger.info("Successfully applied chained postprocess mode: %s", pm)
                        else:
                            logger.warning("Chained postprocess mode %s returned None, keeping previous bytes", pm)
                    except Exception as e:
                        logger.error("Error applying chained postprocess mode %s: %s", pm, e)

            # Detect if the output has an alpha channel — if so, save as PNG to preserve transparency
            from PIL import Image as _PilImage
            try:
                _probe = _PilImage.open(BytesIO(image_bytes))
                has_alpha = _probe.mode in ("RGBA", "LA") or (
                    _probe.mode == "P" and "transparency" in _probe.info
                )
            except Exception:
                has_alpha = False

            if has_alpha:
                mime_type = "image/png"
                ext = "png"
            else:
                mime_type = "image/jpeg"
                ext = "jpeg"

            part = types.Part(
                inline_data=types.Blob(data=image_bytes, mime_type=mime_type)
            )
            artifact_name = (
                f"user:generated_image_{hashlib.md5(prompt.encode()).hexdigest()[:8]}_{int(time.time())}.{ext}"
            )
            logger.info("Saving artifact: %s (mime=%s)", artifact_name, mime_type)
            try:
                await tool_context.save_artifact(artifact_name, part)
                logger.info("Successfully saved artifact: %s", artifact_name)
            except Exception as save_err:
                logger.error("Failed to save artifact %s: %s", artifact_name, save_err)

            # Auto-inject the newly generated image as the current reference canvas
            # so the agent can immediately chain preprocessing tools (like smart_crop)
            tool_context.state["latest_input_image"] = {
                "data": base64.b64encode(image_bytes).decode("utf-8"),
                "mime_type": mime_type,
                "original_prompt": prompt,
            }
            tool_context.state["latest_input_image_artifact"] = artifact_name

            msg = "Image successfully generated and saved. It is now set as the active reference canvas."
            if applied_modes:
                msg += f" Automatically applied processing modes: {', '.join(applied_modes)}."

            return {
                "status": "success",
                "artifact_name": artifact_name,
                "message": msg,
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
    prompt: str = None,
) -> dict:
    """Applies a visual preprocessing transform to the current reference image.
    This replaces the reference image in session state with the processed version,
    so it can be used as a cleaner canvas for the next generate_image call.

    Use this when the user wants to:
    - Strip away texture/detail before reimagining ('trace this', 'outline this', 'use the structure of')
    - Create an abstract version before a style transfer
    - Chain preprocessing with generation: call this first, then call generate_image
    
    IMPORTANT: For FINAL stylistic filters like the Riso modes (), do NOT call `generate_image` after. Just call this tool and stop, telling the user you've applied the filter!

    Args:
        mode: The preprocessing transform to apply:
            - 'canny': Extract edge outlines only (black lines on white). Best for structure-preserving generation.
            - 'sketch': Pencil sketch effect. Removes texture, keeps line work.
            - 'posterize': Reduce color complexity. Keeps shapes, removes noise and fine detail.
            - 'blur': Heavy Gaussian blur. Very abstract canvas — loose vibe reference.
            - 'smart_crop': Intelligently crops to the main subject.
            - 'rembg': Removes the background (legacy, may fail on new Python).
            - 'remove_bg_gemini': Removes the background using an AI-generated silhouette mask. Best for photos and complex scenes with natural edges.
            - 'custom_mask_gemini': Removes specific elements based on the `prompt`. Specify what to keep/remove in the prompt (e.g. 'keep the head, remove the body and arm').
            - 'remove_whitespace': Removes white and near-white pixels (chroma-key). Instant, no API call. Best for flat graphics, logos, icons, and emoji-style art with solid white backgrounds.
            - 'remove_text': Uses AI to remove typography/text while preserving the scene.    Returns:
        A dict with status and the artifact name of the preprocessed image (shown to user as a preview).
    """
    import base64
    import time
    from google.genai import types
    import random
    
    latest_img = tool_context.state.get("latest_input_image")
    if not latest_img:
        return {
            "status": "error",
            "message": "No reference image found in session. Please upload or generate an image first.",
        }

    try:
        raw_bytes = base64.b64decode(latest_img["data"])
        mode = mode.lower().strip()

        processed_bytes = await preprocess_image_bytes(raw_bytes, mode, prompt=prompt)
        if not processed_bytes:
            return {
                "status": "error",
                "message": f"Unknown mode '{mode}' or processing failed. Valid options: canny, sketch, posterize, blur, smart_crop, rembg, custom_mask_gemini, remove_text.",
            }

        processed_b64 = base64.b64encode(processed_bytes).decode("utf-8")

        # Replace the session reference image with the processed version
        tool_context.state["latest_input_image"] = {
            "data": processed_b64,
            "mime_type": "image/png",
        }

        # Save as artifact so Discord shows the preprocessing result as a preview
        part = types.Part(
            inline_data=types.Blob(data=processed_bytes, mime_type="image/png")
        )
        artifact_name = f"user:preprocessed_{mode}_{int(time.time())}.png"
        try:
            await tool_context.save_artifact(artifact_name, part)
        except Exception as save_err:
            logger.error("Failed to save preprocessed artifact: %s", save_err)

        if False:
            return {
                "status": "success",
                "artifact_name": artifact_name,
                "message": (
                    f"Filter '{mode}' applied successfully! Stop and show the user this final output artifact. "
                    "Do NOT call generate_image. The chat UI will automatically attach the Reroll button."
                ),
                "mode": mode,
            }

        return {
            "status": "success",
            "artifact_name": artifact_name,
            "message": (
                f"Image preprocessed with '{mode}' mode and set as the active reference. "
                "Call generate_image to continue."
            ),
            "mode": mode,
        }

    except Exception as e:
        logger.error("Preprocessing error (mode=%s): %s", mode, e)
        return {"status": "error", "message": f"Preprocessing failed: {e}"}


async def preprocess_image_bytes(raw_bytes: bytes, mode: str, prompt: str = None) -> bytes | None:
    """Standalone preprocessing helper: takes raw image bytes, applies a transform,
    returns processed bytes (PNG). No ToolContext required — used by Discord UI buttons.

    Args:
        raw_bytes: Raw image bytes (any PIL-supported format).
        mode: The preprocessing mode to apply.
        prompt: Optional text prompt for modes that require it (e.g. custom_mask_gemini).

    Returns:
        Processed image as PNG bytes, or None on failure.
    """
    import io as _io
    try:
        pil_img = Image.open(_io.BytesIO(raw_bytes))

        if mode in ("rembg", "smart_crop"):
            # Use Gemini 3.1 Flash to get bounding box of main subject
            from google import genai
            import os
            import json
            import re
            
            api_key = os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=api_key)
            
            prompt = "Return a JSON list representing the 2D bounding box of the primary foreground subject in this image in the format [ymin, xmin, ymax, xmax], normalized from 0 to 1000. Output ONLY the JSON list, nothing else."
            
            import base64
            b64_data = base64.b64encode(raw_bytes).decode("utf-8")
            interaction = await client.aio.interactions.create(
                model="gemini-3.1-flash-lite",
                input=[
                    {"type": "text", "text": prompt},
                    {"type": "image", "data": b64_data, "mime_type": "image/png"}
                ],
            )
            
            response_text = getattr(interaction, "output_text", "") or ""
            
            match = re.search(r"\[\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\]", response_text)
            if not match:
                raise ValueError(f"Could not parse bounding box from Gemini response: {response_text}")
            
            box = json.loads(match.group(0))
            ymin, xmin, ymax, xmax = box
            
            width, height = pil_img.size
            x1 = int((xmin / 1000) * width)
            y1 = int((ymin / 1000) * height)
            x2 = int((xmax / 1000) * width)
            y2 = int((ymax / 1000) * height)
            
            # Ensure valid rectangle
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            
            if mode == "smart_crop":
                processed = pil_img.crop((x1, y1, x2, y2))
            
            elif mode == "rembg":
                import cv2
                import numpy as _np
                import asyncio
                
                img_array = _np.array(pil_img.convert("RGB"))
                rect = (x1, y1, x2 - x1, y2 - y1)
                
                # If rectangle is invalid, fallback to full image
                if rect[2] <= 0 or rect[3] <= 0:
                    rect = (1, 1, width - 2, height - 2)
                    
                def _run_grabcut(img, r):
                    mask = _np.zeros(img.shape[:2], _np.uint8)
                    bgdModel = _np.zeros((1, 65), _np.float64)
                    fgdModel = _np.zeros((1, 65), _np.float64)
                    cv2.grabCut(img, mask, r, bgdModel, fgdModel, 5, cv2.GC_INIT_WITH_RECT)
                    return _np.where((mask == 2) | (mask == 0), 0, 1).astype('uint8')
                    
                mask2 = await asyncio.to_thread(_run_grabcut, img_array, rect)
                
                
                # Create RGBA image
                rgba_array = _np.zeros((height, width, 4), dtype=_np.uint8)
                rgba_array[:, :, :3] = img_array
                rgba_array[:, :, 3] = mask2 * 255
                processed = Image.fromarray(rgba_array, "RGBA")

        elif mode == "remove_bg_gemini":
            from google import genai
            import os
            import base64
            import asyncio
            import numpy as _np

            api_key = os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=api_key)

            b64_data = base64.b64encode(raw_bytes).decode("utf-8")

            # Ask Gemini to produce a clean B&W silhouette mask of the foreground subject.
            # White = keep (subject), Black = discard (background).
            mask_interaction = await client.aio.interactions.create(
                model="gemini-3.1-flash-image",
                input=[
                    {
                        "type": "text",
                        "text": (
                            "Output a pure black and white silhouette mask for this image. "
                            "The foreground subject must be solid white. "
                            "The entire background must be pure black. "
                            "CRITICAL: The entire interior of the foreground subject MUST be a completely solid white silhouette! "
                            "Do NOT leave black holes for internal shadows, dark shading, or black ink lines. Fill them in solid white. "
                            "No gray tones, no gradients, no anti-aliasing. "
                            "Just a clean, completely solid binary mask."
                        ),
                    },
                    {"type": "image", "data": b64_data, "mime_type": "image/png"},
                ],
            )

            if not mask_interaction.output_image:
                raise ValueError("Gemini did not return a mask image for remove_bg_gemini.")

            mask_bytes = base64.b64decode(mask_interaction.output_image.data)
            mask_pil = Image.open(_io.BytesIO(mask_bytes)).convert("L").resize(pil_img.size)

            # Hard-threshold the mask to binary — eliminates gray anti-aliasing
            # that causes color washing when used as an alpha channel.
            mask_arr = _np.array(mask_pil)
            mask_arr = (_np.array(mask_arr) > 128).astype(_np.uint8) * 255
            mask_pil = Image.fromarray(mask_arr, "L")

            # Apply mask as real alpha channel to original image
            original_rgba = pil_img.convert("RGBA")
            original_rgba.putalpha(mask_pil)
            processed = original_rgba

        elif mode == "custom_mask_gemini":
            from google import genai
            import os
            import base64
            import asyncio
            import numpy as _np

            api_key = os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=api_key)

            b64_data = base64.b64encode(raw_bytes).decode("utf-8")
            
            mask_prompt = prompt or "Keep the main subject, remove everything else."

            mask_interaction = await client.aio.interactions.create(
                model="gemini-3.1-flash-image",
                input=[
                    {
                        "type": "text",
                        "text": (
                            "Output a pure black and white silhouette mask for this image based on the user's prompt. "
                            f"User prompt: '{mask_prompt}'\n"
                            "The elements to keep must be solid white. The elements to remove/discard must be pure black. "
                            "CRITICAL: The entire interior of the elements you keep MUST be a completely solid white silhouette! "
                            "Do NOT leave black holes for internal shadows, dark shading, or black ink lines. Fill them in solid white. "
                            "No gray tones, no gradients, no anti-aliasing. "
                            "Just a clean, completely solid binary mask."
                        ),
                    },
                    {"type": "image", "data": b64_data, "mime_type": "image/png"},
                ],
            )

            if not mask_interaction.output_image:
                raise ValueError("Gemini did not return a mask image for custom_mask_gemini.")

            mask_bytes = base64.b64decode(mask_interaction.output_image.data)
            mask_pil = Image.open(_io.BytesIO(mask_bytes)).convert("L").resize(pil_img.size)

            mask_arr = _np.array(mask_pil)
            mask_arr = (_np.array(mask_arr) > 128).astype(_np.uint8) * 255
            mask_pil = Image.fromarray(mask_arr, "L")

            original_rgba = pil_img.convert("RGBA")
            original_rgba.putalpha(mask_pil)
            processed = original_rgba

        elif mode == "remove_whitespace":
            # Chroma-key for white/near-white pixels — no API call needed.
            # Perfect for flat-color graphics, logos, icons, and emoji-style art
            # where the background is a solid white (or near-white) field.
            # Any pixel with R>220, G>220, B>220 becomes fully transparent.
            import numpy as _np

            rgba = pil_img.convert("RGBA")
            data = _np.array(rgba, dtype=_np.uint8)
            r, g, b, a = data[..., 0], data[..., 1], data[..., 2], data[..., 3]
            white_mask = (r > 220) & (g > 220) & (b > 220)
            data[white_mask, 3] = 0  # make near-white pixels fully transparent
            processed = Image.fromarray(data, "RGBA")


        elif mode == "remove_text":
            from google import genai
            import os
            import base64
            
            api_key = os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=api_key)
            
            b64_data = base64.b64encode(raw_bytes).decode("utf-8")
            interaction = await client.aio.interactions.create(
                model="gemini-3.1-flash-image",
                input=[
                    {"type": "text", "text": "Remove all text and typography from this image. Preserve the exact scene, characters, layout, background, style, and composition perfectly without adding any new elements."},
                    {"type": "image", "data": b64_data, "mime_type": "image/png"}
                ],
            )
            
            generated_image = interaction.output_image
            if not generated_image:
                raise ValueError("No image generated by Gemini.")
                
            output_bytes = base64.b64decode(generated_image.data)
            processed = Image.open(_io.BytesIO(output_bytes))

        elif mode == "canny":
            pil_img = pil_img.convert("RGB")
            try:
                import cv2
                import numpy as _np
                img_array = _np.array(pil_img)
                gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
                edges = cv2.Canny(gray, threshold1=50, threshold2=150)
                processed = Image.fromarray(edges).convert("RGB")
            except ImportError:
                logger.warning("opencv not installed — falling back to sketch for canny mode")
                # Sketch fallback
                gray = pil_img.convert("L")
                inverted = ImageOps.invert(gray)
                blurred = inverted.filter(ImageFilter.GaussianBlur(radius=10))
                import numpy as _np
                gray_arr = _np.array(gray, dtype=_np.float32)
                blur_arr = _np.array(blurred, dtype=_np.float32)
                sketch_arr = _np.clip(
                    (gray_arr * 255.0) / (255.0 - blur_arr + 1e-6), 0, 255
                ).astype(_np.uint8)
                processed = Image.fromarray(sketch_arr).convert("RGB")

        elif mode == "sketch":
            import numpy as _np
            gray = pil_img.convert("L")
            inverted = ImageOps.invert(gray)
            blurred = inverted.filter(ImageFilter.GaussianBlur(radius=10))
            gray_arr = _np.array(gray, dtype=_np.float32)
            blur_arr = _np.array(blurred, dtype=_np.float32)
            sketch_arr = _np.clip(
                (gray_arr * 255.0) / (255.0 - blur_arr + 1e-6), 0, 255
            ).astype(_np.uint8)
            processed = Image.fromarray(sketch_arr).convert("RGB")

        elif mode == "posterize":
            processed = ImageOps.posterize(pil_img, bits=3)

        elif mode == "blur":
            processed = pil_img.filter(ImageFilter.GaussianBlur(radius=12))

        else:
            logger.error("Unknown preprocess mode: %s", mode)
            return None

        buf = _io.BytesIO()
        processed.save(buf, format="PNG")
        return buf.getvalue()

    except Exception as e:
        logger.error("preprocess_image_bytes error (mode=%s): %s", mode, e)
        return None


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
    prompt_fidelity: str = None,
) -> dict:
    """Sets persistent default values for image generation in the current session.
    These defaults are automatically applied to subsequent generate_image calls.

    Args:
        model: Model code ('gemini-3.1-flash-lite-image', 'gemini-3.1-flash-image', 'gemini-3-pro-image').
        resolution: Resolution ('0.5k', '1k', '2k', '4k').
        aspect_ratio: Aspect ratio (e.g. '1:1', '16:9', '9:16').
        seed: Integer seed for reproducibility. Pass -1 to clear the current seed.
        temperature: Creativity dial (0.0-1.0). Pass -1 to clear and use model default.
        prompt_fidelity: How much the agent elaborates on your prompt:
            - 'literal': Trust your tokens exactly. Proper nouns and artist names are passed as-is.
              Never describe what a proper noun implies. Only add technical quality suffixes.
            - 'guided': Add compositional context if clearly missing (lighting, medium, framing).
              Treat all proper nouns and artist names as sacred anchors — never describe their implied attributes.
            - 'creative': Agent has full elaboration freedom. Good for vague or lazy prompts.
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
    if prompt_fidelity in ("literal", "guided", "creative"):
        tool_context.state["prompt_fidelity"] = prompt_fidelity

    return {"status": "success", "message": "Image defaults updated."}
