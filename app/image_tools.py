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

async def generate_image(
    prompt: str,
    tool_context: ToolContext,
    resolution: str = None,
    aspect_ratio: str = None,
    model: str = None,
    edit_mode: str = "reference",
    seed: int = None,
    temperature: float = None,
    enable_search_grounding: bool = False,
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
    
    IMPORTANT: For FINAL stylistic filters like the Riso modes ('riso_sticker', 'riso_duotone', 'riso_multiply'), do NOT call `generate_image` after. Just call this tool and stop, telling the user you've applied the filter!

    Args:
        mode: The preprocessing transform to apply:
            - 'canny': Extract edge outlines only (black lines on white). Best for structure-preserving generation.
            - 'sketch': Pencil sketch effect. Removes texture, keeps line work.
            - 'posterize': Reduce color complexity. Keeps shapes, removes noise and fine detail.
            - 'blur': Heavy Gaussian blur. Very abstract canvas — loose vibe reference.
            - 'smart_crop': Intelligently crops to the main subject.
            - 'rembg': Removes the background.
            - 'remove_text': Uses AI to remove typography/text while preserving the scene.
            - 'riso_sticker': Applies a Risograph print aesthetic (Sticker style).
            - 'riso_duotone': Applies a Risograph print aesthetic (Duotone style).
            - 'riso_multiply': Applies a Risograph print aesthetic (Multiply style).

    Returns:
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
        
        if mode == "riso_pop":
            mode = random.choice(["riso_sticker", "riso_duotone", "riso_multiply"])

        processed_bytes = await preprocess_image_bytes(raw_bytes, mode)
        if not processed_bytes:
            return {
                "status": "error",
                "message": f"Unknown mode '{mode}' or processing failed. Valid options: canny, sketch, posterize, blur, smart_crop, rembg, remove_text, riso_sticker, riso_duotone, riso_multiply.",
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

        if mode.startswith("riso_"):
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


async def preprocess_image_bytes(raw_bytes: bytes, mode: str) -> bytes | None:
    """Standalone preprocessing helper: takes raw image bytes, applies a transform,
    returns processed bytes (PNG). No ToolContext required — used by Discord UI buttons.

    Args:
        raw_bytes: Raw image bytes (any PIL-supported format).
        mode: Transform to apply — 'canny', 'sketch', 'posterize', or 'blur'.

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
                
                img_array = _np.array(pil_img.convert("RGB"))
                mask = _np.zeros(img_array.shape[:2], _np.uint8)
                bgdModel = _np.zeros((1, 65), _np.float64)
                fgdModel = _np.zeros((1, 65), _np.float64)
                
                rect = (x1, y1, x2 - x1, y2 - y1)
                
                # If rectangle is invalid, fallback to full image
                if rect[2] <= 0 or rect[3] <= 0:
                    rect = (1, 1, width - 2, height - 2)
                    
                cv2.grabCut(img_array, mask, rect, bgdModel, fgdModel, 5, cv2.GC_INIT_WITH_RECT)
                mask2 = _np.where((mask == 2) | (mask == 0), 0, 1).astype('uint8')
                
                # Create RGBA image
                rgba_array = _np.zeros((height, width, 4), dtype=_np.uint8)
                rgba_array[:, :, :3] = img_array
                rgba_array[:, :, 3] = mask2 * 255
                processed = Image.fromarray(rgba_array, "RGBA")

        elif mode == "riso_pop":
            from google import genai
            import os
            import json
            import re
            import random
            import cv2
            import numpy as _np
            import random
            
            api_key = os.getenv("GEMINI_API_KEY")
            client = genai.Client(api_key=api_key)
            
            prompt = (
                "Analyze this image and find the main subjects or characters (up to 3). "
                "For each subject, return a bounding box in the format [ymin, xmin, ymax, xmax]. "
                "The coordinates must be integers from 0 to 1000. "
                "Return ONLY a JSON list of lists, e.g. [[ymin, xmin, ymax, xmax], [ymin, xmin, ymax, xmax]]."
            )
            
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
            match = re.search(r"\[\s*\[.*?\]\s*\]", response_text, re.DOTALL)
            if not match:
                raise ValueError(f"Could not parse bounding boxes from Gemini response: {response_text}")
                
            boxes = json.loads(match.group(0))[:3]
            width, height = pil_img.size
            img_array = _np.array(pil_img.convert("RGB"))
            subject_masks = []
            
            for box in boxes:
                ymin, xmin, ymax, xmax = box
                x1, y1 = max(0, int((xmin / 1000) * width)), max(0, int((ymin / 1000) * height))
                x2, y2 = min(width, int((xmax / 1000) * width)), min(height, int((ymax / 1000) * height))
                rect = (x1, y1, x2 - x1, y2 - y1)
                if rect[2] <= 0 or rect[3] <= 0: continue
                mask = _np.zeros(img_array.shape[:2], _np.uint8)
                bgdModel = _np.zeros((1, 65), _np.float64)
                fgdModel = _np.zeros((1, 65), _np.float64)
                cv2.grabCut(img_array, mask, rect, bgdModel, fgdModel, 5, cv2.GC_INIT_WITH_RECT)
                subject_masks.append(_np.where((mask == 2) | (mask == 0), 0, 1).astype('uint8'))
                
            riso_colors = [
                (0, 168, 225), (230, 0, 126), (255, 237, 0), (255, 72, 176),
                (241, 80, 96), (130, 216, 213), (118, 91, 167), (255, 108, 47),
                (60, 60, 60), (0, 120, 191), (0, 169, 92)
            ]
            
            def get_dominant_color(rgb_arr, mask_arr):
                fg_pixels = rgb_arr[mask_arr == 1]
                if len(fg_pixels) == 0: return (128, 128, 128)
                avg = _np.mean(fg_pixels, axis=0)
                return (int(avg[0]), int(avg[1]), int(avg[2]))

            def color_distance_hue(rgb1, rgb2):
                h1, _, _ = colorsys.rgb_to_hsv(rgb1[0]/255.0, rgb1[1]/255.0, rgb1[2]/255.0)
                h2, _, _ = colorsys.rgb_to_hsv(rgb2[0]/255.0, rgb2[1]/255.0, rgb2[2]/255.0)
                diff = abs(h1 - h2)
                return min(diff, 1.0 - diff)
            
            def create_dithered_layer(rgb_arr, mask_arr, color_tuple=None):
                layer_rgb = rgb_arr.copy()
                layer_rgb[mask_arr == 0] = [255, 255, 255]
                layer_pil = Image.fromarray(layer_rgb).convert("L")
                from PIL import ImageEnhance
                layer_pil = ImageEnhance.Contrast(layer_pil).enhance(1.8).convert("1").convert("L")
                dither_arr = _np.array(layer_pil)
                rgba = _np.zeros((height, width, 4), dtype=_np.uint8)
                if color_tuple is None:
                    rgba[dither_arr < 128] = [20, 20, 20, 255]
                else:
                    rgba[dither_arr < 128] = [*color_tuple, 255]
                return Image.fromarray(rgba, "RGBA")
            
            bg_mask = 1 - combined_fg_mask
            
            canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
            canvas.alpha_composite(create_dithered_layer(img_array, bg_mask, None))
            
            for i, s_mask in enumerate(subject_masks):
                avg_rgb = get_dominant_color(img_array, s_mask)
                h, _, _ = colorsys.rgb_to_hsv(avg_rgb[0]/255.0, avg_rgb[1]/255.0, avg_rgb[2]/255.0)
                
                if mode == "riso_sticker":
                    target_hue = (h + 0.5) % 1.0
                    target_rgb = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(target_hue, 1, 1))
                    s_color = min(riso_colors, key=lambda c: color_distance_hue(c, target_rgb))
                    sticker_bg = _np.zeros((height, width, 4), dtype=_np.uint8)
                    sticker_bg[s_mask == 1] = [*s_color, 255]
                    canvas.alpha_composite(Image.fromarray(sticker_bg, "RGBA"))
                    canvas.alpha_composite(create_dithered_layer(img_array, s_mask, (20, 20, 20)))
                    
                elif mode == "riso_duotone":
                    c1 = min(riso_colors, key=lambda c: color_distance_hue(c, avg_rgb))
                    target_rgb = tuple(int(x * 255) for x in colorsys.hsv_to_rgb((h + 0.1) % 1.0, 1, 1))
                    c2 = min(riso_colors, key=lambda c: color_distance_hue(c, target_rgb))
                    c_dark, c_light = sorted([c1, c2], key=lambda c: 0.299*c[0] + 0.587*c[1] + 0.114*c[2])
                    layer_rgb = img_array.copy()
                    layer_rgb[s_mask == 0] = [255, 255, 255]
                    dither_1 = _np.array(Image.fromarray(layer_rgb).convert("L").convert("1").convert("L"))
                    rgba = _np.zeros((height, width, 4), dtype=_np.uint8)
                    rgba[(dither_1 < 128) & (s_mask == 1)] = [*c_dark, 255]
                    rgba[(dither_1 >= 128) & (s_mask == 1)] = [*c_light, 255]
                    canvas.alpha_composite(Image.fromarray(rgba, "RGBA"))
                    
                elif mode == "riso_multiply":
                    c_neon = min(riso_colors, key=lambda c: color_distance_hue(c, avg_rgb))
                    layer_rgb = img_array.copy()
                    layer_rgb[s_mask == 0] = [255, 255, 255]
                    gray_arr = _np.array(Image.fromarray(layer_rgb).convert("L").convert("RGB")).astype(_np.float32) / 255.0
                    rgba = _np.zeros((height, width, 4), dtype=_np.uint8)
                    rgba[..., :3] = (gray_arr * _np.array(c_neon, dtype=_np.float32)).astype(_np.uint8)
                    rgba[..., 3] = s_mask * 255
                    canvas.alpha_composite(Image.fromarray(rgba, "RGBA"))
            processed = canvas.convert("RGB")

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
