"""Discord UI components for Sophee.

Consolidates ImageView, ImageEditModal, RadioView, and SkipView.
Deduplicates the image artifact detection + thread creation pattern.
"""

import asyncio
import base64
import logging
import os
import random
import tempfile

import discord
import requests
from google.genai import types

from bot.cache import get_image_metadata, save_image_metadata
from bot.message_utils import bracket_urls, send_message_in_chunks

logger = logging.getLogger("sophee.bot.views")

LASTFM_KEY = os.getenv("LASTFM_KEY")


def create_user_profile_embed(user_prefs: dict) -> discord.Embed:
    """Creates a Discord Embed listing the user's recorded personalization statements."""
    embed = discord.Embed(
        title="👤 Sophee Personalization Profile",
        description="Sophee records behavioral corrections and statements about your preferences to personalize your experience.",
        color=discord.Color.blue(),
    )
    
    corrections = user_prefs.get("corrections", [])
    if corrections:
        prefs_str = "\n".join(f"**{i}.** {c}" for i, c in enumerate(corrections, 1))
        embed.add_field(name="Recorded Preferences & Corrections", value=prefs_str, inline=False)
        embed.set_footer(text="To remove a preference, say 'delete preference <number>'.")
    else:
        embed.add_field(
            name="Recorded Preferences & Corrections",
            value="*No preferences recorded yet.*\nCorrect my behavior, share your likes/dislikes, or tell me what to remember, and they will appear here!",
            inline=False
        )
        embed.set_footer(text="Sophee will automatically build your profile as you chat.")
        
    return embed


# ---------------------------------------------------------------------------
# Image Settings View
# ---------------------------------------------------------------------------

class ImageSettingsView(discord.ui.View):
    def __init__(self, session_state: dict, update_state_fn, user_id: str, session_id: str):
        super().__init__(timeout=None)
        self.session_state = session_state
        self.update_state_fn = update_state_fn
        self.user_id = user_id
        self.session_id = session_id
        
        # Determine defaults
        current_model = self.session_state.get("default_image_model", "gemini-3.1-flash-lite-image")
        current_res = self.session_state.get("default_image_resolution", "0.5k")
        current_ratio = self.session_state.get("default_image_ratio", "1:1")

        # Set default values for dropdowns
        for opt in self.model_select.options:
            if opt.value == current_model: opt.default = True
        for opt in self.resolution_select.options:
            if opt.value == current_res: opt.default = True
        for opt in self.ratio_select.options:
            if opt.value == current_ratio: opt.default = True

    async def _update_and_refresh(self, interaction: discord.Interaction):
        await self.update_state_fn(self.user_id, self.session_id, self.session_state)
        embed, view = create_image_settings_view(self.session_state, self.user_id, self.session_id, self.update_state_fn)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.select(
        placeholder="Select Model",
        options=[
            discord.SelectOption(label="Lite (Fast)", value="gemini-3.1-flash-lite-image", description="gemini-3.1-flash-lite-image"),
            discord.SelectOption(label="Flash (Grounding)", value="gemini-3.1-flash-image", description="gemini-3.1-flash-image"),
            discord.SelectOption(label="Pro (High Quality)", value="gemini-3-pro-image", description="gemini-3-pro-image"),
        ]
    )
    async def model_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.session_state["default_image_model"] = select.values[0]
        await self._update_and_refresh(interaction)

    @discord.ui.select(
        placeholder="Select Resolution",
        options=[
            discord.SelectOption(label="0.5k (512x512)", value="0.5k"),
            discord.SelectOption(label="1k (1024x1024)", value="1k"),
            discord.SelectOption(label="2k (2048x2048)", value="2k"),
            discord.SelectOption(label="4k (4096x4096)", value="4k"),
        ]
    )
    async def resolution_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.session_state["default_image_resolution"] = select.values[0]
        await self._update_and_refresh(interaction)

    @discord.ui.select(
        placeholder="Select Aspect Ratio",
        options=[
            discord.SelectOption(label="1:1 (Square)", value="1:1"),
            discord.SelectOption(label="16:9 (Widescreen)", value="16:9"),
            discord.SelectOption(label="9:16 (Vertical)", value="9:16"),
            discord.SelectOption(label="4:3 (Standard)", value="4:3"),
            discord.SelectOption(label="3:4 (Portrait)", value="3:4"),
            discord.SelectOption(label="3:2", value="3:2"),
            discord.SelectOption(label="2:3", value="2:3"),
            discord.SelectOption(label="4:5", value="4:5"),
            discord.SelectOption(label="5:4", value="5:4"),
            discord.SelectOption(label="21:9 (Ultrawide)", value="21:9"),
        ]
    )
    async def ratio_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.session_state["default_image_ratio"] = select.values[0]
        await self._update_and_refresh(interaction)

def create_image_settings_view(session_state: dict, user_id: str, session_id: str, update_state_fn) -> tuple[discord.Embed, discord.ui.View]:
    embed = discord.Embed(
        title="🎨 Image Generation Settings",
        description="Configure defaults for image generation. These settings will expire when the session is cleansed or goes idle for 4 hours.",
        color=discord.Color.purple()
    )
    
    current_model = session_state.get("default_image_model", "gemini-3.1-flash-lite-image")
    current_res = session_state.get("default_image_resolution", "0.5k")
    current_ratio = session_state.get("default_image_ratio", "1:1")
    
    embed.add_field(name="Current Defaults", value=f"**Model:** `{current_model}`\n**Resolution:** `{current_res}`\n**Aspect Ratio:** `{current_ratio}`", inline=False)
    
    last_settings = session_state.get("last_image_settings")
    if last_settings:
        model = last_settings.get("model", "N/A")
        res = last_settings.get("resolution", "N/A")
        ratio = last_settings.get("aspect_ratio", "N/A")
        grounded = "Yes" if last_settings.get("grounding_enabled") else "No"
        ref_image = "Yes" if last_settings.get("has_image") else "No"
        prompt = last_settings.get("prompt", "N/A")
        
        last_str = (
            f"**Model:** `{model}`\n"
            f"**Resolution:** `{res}`\n"
            f"**Aspect Ratio:** `{ratio}`\n"
            f"**Search Grounding:** {grounded}\n"
            f"**Reference Image Used:** {ref_image}\n"
            f"**Prompt Used:**\n> {prompt}"
        )
        embed.add_field(name="Last Generation Details", value=last_str, inline=False)
    else:
        embed.add_field(name="Last Generation Details", value="*No images generated yet in this session.*", inline=False)
        
    view = ImageSettingsView(session_state, update_state_fn, user_id, session_id)
    return embed, view



# ---------------------------------------------------------------------------
# Shared helper: run agent and extract image artifact
# ---------------------------------------------------------------------------

async def _run_agent_and_get_image(
    runner, artifact_service, user_id, session_id, prompt_text, image_bytes=None, image_mime="image/png"
):
    """Runs the agent with a prompt (and optional image) and returns (temp_file_path, response_text, new_image_key)."""
    before_keys = set(
        await artifact_service.list_artifact_keys(
            app_name="app", user_id=user_id, session_id=session_id
        )
    )

    parts = [types.Part.from_text(text=prompt_text)]
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    response_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=parts
        ),
    ):
        if event.is_final_response():
            response_parts = (
                event.content.parts
                if (event.content and event.content.parts)
                else []
            )
            response_text += "".join([p.text for p in response_parts if p.text])

    after_keys = set(
        await artifact_service.list_artifact_keys(
            app_name="app", user_id=user_id, session_id=session_id
        )
    )
    new_keys = after_keys - before_keys

    new_image_key = None
    for key in new_keys:
        if key.endswith((".jpeg", ".jpg", ".png")):
            new_image_key = key
            break

    if new_image_key:
        part = await artifact_service.load_artifact(
            app_name="app",
            user_id=user_id,
            filename=new_image_key,
            session_id=session_id,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg", mode="wb") as f:
            f.write(part.inline_data.data)
            temp_file_path = f.name
        return temp_file_path, response_text, new_image_key

    return None, response_text, None


async def _restyle_image_direct(image_bytes: bytes, image_mime: str, style_str: str, resolution: str = "0.5k") -> tuple:
    """Directly calls the Interactions API with image + style string. No agent, no prompt rewriting.
    Returns (temp_file_path, new_image_key, error_msg).
    """
    import google.genai as genai
    import hashlib

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)

    api_image_size = "1K" if resolution.lower().strip() == "1k" else "512"

    input_data = [
        {
            "type": "image",
            "data": base64.b64encode(image_bytes).decode("utf-8"),
            "mime_type": image_mime,
        },
        {
            "type": "text",
            "text": style_str,
        },
    ]

    try:
        interaction = await client.aio.interactions.create(
            model="gemini-3.1-flash-image",
            input=input_data,
            response_format={"type": "image", "image_size": api_image_size},
        )
        generated = interaction.output_image
        if not generated:
            logger.warning("Interactions API returned 200 OK but no output_image. Interaction dump: %s", str(interaction))
            # Try to dig out a finish reason if available
            finish_reason = "Unknown safety filter or blocked by policy."
            if hasattr(interaction, "candidates") and interaction.candidates:
                cand = interaction.candidates[0]
                if hasattr(cand, "finish_reason"):
                    finish_reason = str(cand.finish_reason)
            return None, None, f"Blocked: {finish_reason}"

        result_bytes = base64.b64decode(generated.data)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg", mode="wb") as f:
            f.write(result_bytes)
            return f.name, f"restyle_{hashlib.md5(style_str.encode()).hexdigest()[:8]}.jpeg", None
    except Exception as e:
        logger.error("Error in _restyle_image_direct: %s", e)
        return None, None, str(e)


async def _run_agent_and_get_text(runner, user_id, session_id, prompt_text):
    """Runs the agent with a prompt and returns the text response."""
    response_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=prompt_text)]
        ),
    ):
        if event.is_final_response():
            response_parts = (
                event.content.parts
                if (event.content and event.content.parts)
                else []
            )
            response_text += "".join([p.text for p in response_parts if p.text])
    return response_text


async def _send_image_with_thread(
    interaction_or_message, temp_file_path, response_text, view, label="Image Details",
    *, is_followup=True,
):
    """Sends an image file with a view and creates an archived thread for details."""
    if is_followup:
        sent_msg = await interaction_or_message.followup.send(
            content=None, file=discord.File(temp_file_path), view=view,
        )
    else:
        sent_msg = await interaction_or_message.reply(
            content=None, file=discord.File(temp_file_path), view=view,
        )
    os.remove(temp_file_path)

    if response_text:
        try:
            fetched_msg = await interaction_or_message.channel.fetch_message(sent_msg.id) if is_followup else sent_msg
            active_thread = await fetched_msg.create_thread(name=label)
            await send_message_in_chunks(active_thread, response_text, is_thread=True)
            await active_thread.edit(archived=True)
        except Exception as thread_err:
            logger.warning("Error creating thread: %s", thread_err)
            channel = interaction_or_message.channel if is_followup else interaction_or_message
            await send_message_in_chunks(channel, response_text, is_thread=False)

    return sent_msg


# ---------------------------------------------------------------------------
# Image Edit Modal
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Image View (Edit / Reroll / Restyle buttons)
# ---------------------------------------------------------------------------


async def _trigger_restyle_options(interaction, parent_msg_id, original_prompt, user_id, session_id, runner, artifact_service, session_service, update_state_fn, is_reroll=False):
    import random
    import json
    import os
    
    catalog_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "artists_catalog.json"
    )
    if not os.path.exists(catalog_path):
        await interaction.followup.send("Artists catalog not found.", ephemeral=True)
        return
        
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
            
        mediums = [name for name, cat in catalog.items() if cat == "medium_and_line"]
        lightings = [name for name, cat in catalog.items() if cat == "lighting_and_atmosphere"]
        genres = [name for name, cat in catalog.items() if cat == "genre_and_subject"]
        
        import urllib.parse

        def _google_link(name: str) -> str:
            url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(name + " artist")
            return f"[{name}]({url})"

        style_strings = []
        display_strings = []   # linked version for Discord embed
        for _ in range(3):
            m = random.choice(mediums)
            l = random.choice(lightings)
            g = random.choice(genres)
            style_strings.append(f"art by {m}, {l}, and {g}")
            display_strings.append(f"art by {_google_link(m)}, {_google_link(l)}, and {_google_link(g)}")
            
        prompt = f"""Here are 3 artistic style combinations based on the prompt '{original_prompt}':
1. {style_strings[0]}
2. {style_strings[1]}
3. {style_strings[2]}

For each, write a 1-sentence blurb describing what this visual combination looks like. Return STRICTLY a JSON array of 3 objects, each with 'style_string' (the exact string provided above) and 'blurb'. Use markdown ```json format."""

        # Ensure session exists before running agent
        await update_state_fn(user_id, session_id, {})
        response_text = await _run_agent_and_get_text(runner, user_id, session_id, prompt)
        
        # parse json
        try:
            # strip markdown if present
            if "```json" in response_text:
                json_str = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                json_str = response_text.split("```")[1].strip()
            else:
                json_str = response_text.strip()
                
            styles_data = json.loads(json_str)
        except Exception as parse_e:
            logger.error("Failed to parse JSON for restyle options: %s\nText: %s", parse_e, response_text)
            await interaction.followup.send("Failed to generate style options.", ephemeral=True)
            return

        # Map plain style_string → linked display_string
        display_lookup = {style_strings[i]: display_strings[i] for i in range(len(style_strings))}

        embed = discord.Embed(title="\U0001f58c\ufe0f Choose a New Style", color=0x2b2d31)
        for idx, style_info in enumerate(styles_data):
            plain = style_info.get('style_string', '')
            linked = display_lookup.get(plain, plain)
            # Stamp the linked version onto the dict so the callback can use it in the thread
            style_info['display_string'] = linked
            embed.add_field(name=f"Style {idx+1}", value=f"**{linked}**\n{style_info.get('blurb')}", inline=False)
            
        view = StyleSelectionView(
            styles_data, original_prompt, user_id, session_id,
            runner, artifact_service, session_service, update_state_fn, parent_msg_id
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        
    except Exception as e:
        logger.error("Error generating style options: %s", e)
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


async def trigger_restyle_from_message(
    message,           # discord.Message that triggered the restyle
    ref_msg,           # discord.Message being restyled (has the image)
    original_prompt,   # str — prompt to restyle from
    user_id, session_id, runner, artifact_service, session_service, update_state_fn,
):
    """Triggers the restyle style-picker from a plain message reply (non-ephemeral)."""
    import random, json, os, urllib.parse

    catalog_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "artists_catalog.json"
    )
    if not os.path.exists(catalog_path):
        await message.reply("Artists catalog not found.")
        return

    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)

        mediums   = [name for name, cat in catalog.items() if cat == "medium_and_line"]
        lightings = [name for name, cat in catalog.items() if cat == "lighting_and_atmosphere"]
        genres    = [name for name, cat in catalog.items() if cat == "genre_and_subject"]

        def _google_link(name: str) -> str:
            url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(name + " artist")
            return f"[{name}]({url})"

        style_strings, display_strings = [], []
        for _ in range(3):
            m, l, g = random.choice(mediums), random.choice(lightings), random.choice(genres)
            style_strings.append(f"art by {m}, {l}, and {g}")
            display_strings.append(f"art by {_google_link(m)}, {_google_link(l)}, and {_google_link(g)}")

        prompt = f"""Here are 3 artistic style combinations based on the prompt '{original_prompt}':
1. {style_strings[0]}
2. {style_strings[1]}
3. {style_strings[2]}

For each, write a 1-sentence blurb describing what this visual combination looks like. Return STRICTLY a JSON array of 3 objects, each with 'style_string' (the exact string provided above) and 'blurb'. Use markdown ```json format."""

        await update_state_fn(user_id, session_id, {})
        response_text = await _run_agent_and_get_text(runner, user_id, session_id, prompt)

        try:
            if "```json" in response_text:
                json_str = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                json_str = response_text.split("```")[1].strip()
            else:
                json_str = response_text.strip()
            styles_data = json.loads(json_str)
        except Exception as parse_e:
            logger.error("Failed to parse restyle JSON: %s", parse_e)
            await message.reply("Failed to generate style options.")
            return

        display_lookup = {style_strings[i]: display_strings[i] for i in range(len(style_strings))}

        embed = discord.Embed(
            title="🖌️ Choose a New Style",
            description=f"Restyling image from {ref_msg.author.display_name}",
            color=0x2b2d31,
        )
        for idx, style_info in enumerate(styles_data):
            plain  = style_info.get("style_string", "")
            linked = display_lookup.get(plain, plain)
            style_info["display_string"] = linked
            embed.add_field(name=f"Style {idx+1}", value=f"**{linked}**\n{style_info.get('blurb')}", inline=False)

        view = StyleSelectionView(
            styles_data, original_prompt, user_id, session_id,
            runner, artifact_service, session_service, update_state_fn,
            parent_msg_id=str(ref_msg.id),
        )

        # Download the referenced image so make_callback can pass it to the image gen model
        import aiohttp
        image_bytes = None
        image_mime = "image/png"
        if ref_msg.attachments:
            att = ref_msg.attachments[0]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(att.url) as resp:
                        if resp.status == 200:
                            image_bytes = await resp.read()
                            image_mime = att.content_type or "image/png"
            except Exception as dl_err:
                logger.warning("Could not download image for restyle: %s", dl_err)
        view.image_bytes = image_bytes
        view.image_mime = image_mime

        # Send as a visible reply (not ephemeral — anyone can see and pick a style)
        await message.reply(embed=embed, view=view)

    except Exception as e:
        logger.error("Error in trigger_restyle_from_message: %s", e)
        await message.reply(f"Error generating style options: {e}")

class StyleSelectionView(discord.ui.View):
    def __init__(self, styles_data, original_prompt, user_id, session_id, runner, artifact_service, session_service, update_state_fn, parent_msg_id):
        super().__init__(timeout=None)
        self.styles_data = styles_data
        self.original_prompt = original_prompt
        self.user_id = user_id
        self.session_id = session_id
        self.runner = runner
        self.artifact_service = artifact_service
        self.session_service = session_service
        self.update_state_fn = update_state_fn
        self.parent_msg_id = parent_msg_id
        self.image_bytes = getattr(self, 'image_bytes', None)  # set after init if needed

        # Add buttons for each style dynamically
        for idx, style_info in enumerate(styles_data):
            btn = discord.ui.Button(label=f"Style {idx + 1}", style=discord.ButtonStyle.primary)
            btn.callback = self.make_callback(style_info)
            self.add_item(btn)
        
        reroll_btn = discord.ui.Button(label="Reroll Options", style=discord.ButtonStyle.secondary, emoji="🎲")
        reroll_btn.callback = self.reroll_callback
        self.add_item(reroll_btn)

    def make_callback(self, style_info):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            
            try:
                style_str = style_info.get("style_string", "")
                await self.update_state_fn(self.user_id, self.session_id, {
                    "rolled_style": style_str,
                    "force_style_roll": True,
                    "art_director_mode": "simple",
                    "start_fresh_image": False,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                })

                # We don't edit the ephemeral message to remove the view, so they can press multiple buttons!
                pass

                # Send public placeholder message
                channel = interaction.channel
                try:
                    parent_msg = await channel.fetch_message(int(self.parent_msg_id))
                except:
                    parent_msg = channel
                
                placeholder = None
                if isinstance(parent_msg, discord.Message):
                    placeholder = await parent_msg.reply("🖌️ *Applying new style...*")
                else:
                    placeholder = await channel.send("🖌️ *Applying new style...*")

                # --- Direct restyle: image bytes + style string, no LLM in the middle ---
                image_bytes = self.image_bytes
                image_mime = getattr(self, 'image_mime', 'image/jpeg')

                if image_bytes is None:
                    # Button restyle on bot image: load from artifact store
                    try:
                        meta = await get_image_metadata(str(self.parent_msg_id))
                        artifact_key = meta.get("image_artifact") if meta else None
                        if artifact_key:
                            part = await self.artifact_service.load_artifact(
                                app_name="app",
                                user_id=self.user_id,
                                filename=artifact_key,
                                session_id=self.session_id,
                            )
                            if part and part.inline_data:
                                image_bytes = part.inline_data.data
                                image_mime = part.inline_data.mime_type or "image/jpeg"
                    except Exception as load_err:
                        logger.warning("Could not load artifact for restyle: %s", load_err)

                if image_bytes:
                    temp_path, new_image_key, error_msg = await _restyle_image_direct(
                        image_bytes, image_mime, style_str
                    )
                else:
                    await placeholder.edit(content="Could not load source image for restyle.")
                    return

                await self.update_state_fn(self.user_id, self.session_id, {
                    "force_style_roll": False, "art_director_mode": "simple", "start_fresh_image": False,
                })

                if temp_path:
                    view = ImageView(
                        self.user_id, self.session_id,
                        self.runner, self.artifact_service, self.session_service, self.update_state_fn,
                    )

                    sent_msg = await placeholder.edit(
                        content="🖌️ **Restyled**",
                        attachments=[discord.File(temp_path)],
                        view=view
                    )

                    try:
                        fetched_msg = await channel.fetch_message(sent_msg.id)
                        active_thread = await fetched_msg.create_thread(name="Image Details")
                        from bot.message_utils import send_message_in_chunks
                        display_str = style_info.get('display_string', style_str)
                        await send_message_in_chunks(active_thread, f"**Style:** {display_str}", is_thread=True)
                        await active_thread.edit(archived=True)
                    except Exception as thread_err:
                        logger.warning("Error creating thread: %s", thread_err)

                    os.remove(temp_path)

                    await save_image_metadata(
                        message_id=str(sent_msg.id),
                        prompt=self.original_prompt or "restyled image",
                        style=style_str,
                        resolution="0.5k",
                        image_artifact=new_image_key or "",
                        session_id=self.session_id,
                    )
                else:
                    fail_msg = "Failed to restyle image."
                    if 'error_msg' in locals() and error_msg:
                        fail_msg = f"Failed to restyle image (API Error): {error_msg}"
                    await placeholder.edit(content=fail_msg)

            except Exception as e:
                logger.error("Error generating chosen style: %s", e)
                await interaction.followup.send(f"Error generating style: {e}", ephemeral=True)

        return callback

    async def reroll_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except:
            pass
        await _trigger_restyle_options(
            interaction, self.parent_msg_id, self.original_prompt, self.user_id, self.session_id,
            self.runner, self.artifact_service, self.session_service, self.update_state_fn,
            is_reroll=True
        )


class ImageView(discord.ui.View):
    def __init__(self, user_id, session_id, runner, artifact_service, session_service, update_state_fn):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        self.runner = runner
        self.artifact_service = artifact_service
        self.session_service = session_service
        self.update_state_fn = update_state_fn

    async def _get_main_defaults(self) -> dict:
        main_session = await self.session_service.get_session(app_name="app", user_id=self.user_id, session_id=self.session_id)
        if main_session and main_session.state:
            return {
                "default_image_ratio": main_session.state.get("default_image_ratio", "1:1"),
                "default_image_resolution": main_session.state.get("default_image_resolution", "0.5k"),
                "default_image_model": main_session.state.get("default_image_model", "gemini-3.1-flash-lite-image"),
            }
        return {}

    @discord.ui.button(label="Reroll", style=discord.ButtonStyle.secondary, emoji="\U0001f501")
    async def reroll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            parent_msg_id = str(interaction.message.id)
            ref_meta = await get_image_metadata(parent_msg_id)

            original_prompt = ""
            active_session_id = self.session_id
            main_defaults = await self._get_main_defaults()
            
            if ref_meta:
                original_prompt = ref_meta.get("prompt", "")
                if ref_meta.get("session_id"):
                    active_session_id = ref_meta.get("session_id")
                state_update = {
                    "rolled_style": ref_meta.get("style"),
                    "latest_resolution": ref_meta.get("resolution"),
                    "force_style_roll": False,
                    "start_fresh_image": True,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                }
                state_update.update(main_defaults)
                await self.update_state_fn(self.user_id, active_session_id, state_update)
            else:
                state_update = {
                    "force_style_roll": False,
                    "start_fresh_image": True,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                }
                state_update.update(main_defaults)
                await self.update_state_fn(self.user_id, active_session_id, state_update)

            if original_prompt:
                run_prompt = f"Start fresh and generate a brand new image using the exact same prompt description: '{original_prompt}'. Do not roll a new style; reuse the existing rolled_style if it was active."
            else:
                run_prompt = "Start fresh and generate a brand new image using the exact same prompt description and style settings."

            temp_path, response_text, new_image_key = await _run_agent_and_get_image(
                self.runner, self.artifact_service, self.user_id, active_session_id, run_prompt
            )

            await self.update_state_fn(self.user_id, active_session_id, {
                "force_style_roll": False, "art_director_mode": "simple", "start_fresh_image": False,
            })

            if temp_path:
                view = ImageView(
                    self.user_id, active_session_id,
                    self.runner, self.artifact_service, self.session_service, self.update_state_fn,
                )
                sent_msg = await _send_image_with_thread(
                    interaction, temp_path, f"\U0001f501 **Reroll**\n\n{response_text}", view,
                )

                session = await self.session_service.get_session(
                    app_name="app", user_id=self.user_id, session_id=active_session_id
                )
                last_prompt = session.state.get("last_generated_prompt") if session else None
                await save_image_metadata(
                    message_id=str(sent_msg.id),
                    prompt=last_prompt or original_prompt or "rerolled image",
                    style=session.state.get("rolled_style") if session else None,
                    resolution=session.state.get("latest_resolution", "0.5k") if session else "0.5k",
                    image_artifact=new_image_key,
                    session_id=active_session_id,
                )
            else:
                await interaction.followup.send(response_text or "Failed to reroll image.")
        except Exception as e:
            logger.error("Error in reroll: %s", e)
            await interaction.followup.send(f"Error in reroll: {e}")

    @discord.ui.button(label="Restyle", style=discord.ButtonStyle.secondary, emoji="🖌️")
    async def restyle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            parent_msg_id = str(interaction.message.id)
            ref_meta = await get_image_metadata(parent_msg_id)
            
            original_prompt = ""
            active_session_id = self.session_id
            main_defaults = await self._get_main_defaults()
            
            if ref_meta:
                original_prompt = ref_meta.get("prompt", "")
                if ref_meta.get("session_id"):
                    active_session_id = ref_meta.get("session_id")
            
            if main_defaults:
                await self.update_state_fn(self.user_id, active_session_id, main_defaults)
                    
            if not original_prompt:
                original_prompt = "restyled image"
                
            await _trigger_restyle_options(
                interaction, parent_msg_id, original_prompt, self.user_id, active_session_id,
                self.runner, self.artifact_service, self.session_service, self.update_state_fn
            )
        except Exception as e:
            logger.error("Error triggering restyle: %s", e)
            await interaction.followup.send(f"Error in restyle: {e}")

    @discord.ui.button(label="⚙️", style=discord.ButtonStyle.secondary)
    async def settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from bot.views import create_image_settings_view
        # We need the user's session state
        session = await self.session_service.get_session(app_name="app", user_id=self.user_id, session_id=self.session_id)
        state = session.state if session else {}
        embed, view = create_image_settings_view(state, self.user_id, self.session_id, self.update_state_fn)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Radio View
# ---------------------------------------------------------------------------

async def mutate_playlist_via_lastfm(playlist_data, pool_size=5):
    """For each track in a playlist, fetch similar tracks and randomly replace."""
    mutated_tracks = []
    tracks = playlist_data.get("tracks", [])

    for track in tracks:
        artist = track.get("artist", "")
        title = track.get("title", "")

        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=track.getsimilar"
            f"&artist={requests.utils.quote(artist)}"
            f"&track={requests.utils.quote(title)}"
            f"&api_key={LASTFM_KEY}&format=json&limit={pool_size}"
        )

        try:
            response = await asyncio.to_thread(requests.get, url, timeout=5)
            if response.status_code == 200:
                res_data = response.json()
                similars = res_data.get("similartracks", {}).get("track", [])
                if similars:
                    chosen = random.choice(similars)
                    mutated_tracks.append({
                        "artist": chosen.get("artist", {}).get("name", "Unknown Artist"),
                        "title": chosen.get("name", "Unknown Title"),
                    })
                    continue
        except Exception as e:
            logger.warning("LastFM mutate error for %s - %s: %s", artist, title, e)

        mutated_tracks.append(track)

    return {
        "tracks": mutated_tracks,
        "playlist_thesis": playlist_data.get("playlist_thesis", "music"),
    }


def create_radio_embed(playlist_data):
    """Creates a Discord embed for a radio station playlist."""
    playlist_thesis = playlist_data.get("playlist_thesis", "music")
    if len(playlist_thesis) > 200:
        playlist_thesis = playlist_thesis[:197] + "..."
    embed = discord.Embed(
        title=f"\U0001f4fb {playlist_thesis.title()} Radio Station",
        color=discord.Color.purple(),
    )

    tracks = playlist_data.get("tracks", [])
    for i, track in enumerate(tracks):
        embed.add_field(
            name=f"Track {i + 1}",
            value=f"\U0001f3b5 {track.get('artist', '')} - {track.get('title', '')}",
            inline=False,
        )

    return embed


class RadioView(discord.ui.View):
    def __init__(self, playlist_data, user_id, session_id, session_service, update_state_fn):
        super().__init__(timeout=None)
        self.playlist_data = playlist_data
        self.user_id = user_id
        self.session_id = session_id
        self.session_service = session_service
        self.update_state_fn = update_state_fn
        self.abort_event = asyncio.Event()

    @discord.ui.button(label="Reroll (Smooth)", style=discord.ButtonStyle.secondary, emoji="\U0001f3b2")
    async def reroll_button_smooth(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        mutated_data = await mutate_playlist_via_lastfm(self.playlist_data, pool_size=5)
        self.playlist_data = mutated_data

        session = await self.session_service.get_session(
            app_name="app", user_id=self.user_id, session_id=self.session_id
        )
        if session:
            session.state["playlist"] = mutated_data["tracks"]
            await self.update_state_fn(self.user_id, self.session_id, {"playlist": mutated_data["tracks"]})

        embed = create_radio_embed(mutated_data)
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Reroll (Chaotic)", style=discord.ButtonStyle.secondary, emoji="\U0001f32a\ufe0f")
    async def reroll_button_chaotic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        mutated_data = await mutate_playlist_via_lastfm(self.playlist_data, pool_size=20)
        self.playlist_data = mutated_data

        session = await self.session_service.get_session(
            app_name="app", user_id=self.user_id, session_id=self.session_id
        )
        if session:
            session.state["playlist"] = mutated_data["tracks"]
            await self.update_state_fn(self.user_id, self.session_id, {"playlist": mutated_data["tracks"]})

        embed = create_radio_embed(mutated_data)
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Automate with DJ", style=discord.ButtonStyle.success, emoji="\U0001f399\ufe0f")
    async def automate_dj_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_automation(interaction, use_dj=True)

    @discord.ui.button(label="Pure Music (No DJ)", style=discord.ButtonStyle.primary, emoji="\U0001f3b5")
    async def automate_music_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_automation(interaction, use_dj=False)

    async def start_automation(self, interaction, use_dj):
        # Import here to avoid circular imports
        from bot.audio import audio_player_task, build_radio_sequence
        from app.radio_state import set_radio_state

        if not interaction.user.voice:
            await interaction.response.send_message(
                "\u274c You must be in a voice channel to start the broadcast!",
                ephemeral=True,
            )
            return

        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild.id
        channel_id = interaction.channel.id
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()

        await interaction.response.defer()
        mode_text = "with DJ commentary" if use_dj else "in pure music mode"
        await interaction.followup.send(
            f"\U0001f399\ufe0f Connected to **{voice_channel.name}**. Starting the broadcast {mode_text}..."
        )

        # Populate the shared radio state
        from app.radio_state import active_radios
        existing_state = active_radios.get(guild_id, {})
        
        state_dict = {
            **existing_state, # Preserve manually configured settings like jit_enabled and mode
            "active": True,
            "playlist_thesis": self.playlist_data.get("playlist_thesis", "music"),
            "genre": self.playlist_data.get("playlist_thesis", "music"),
            "upcoming_tracks": list(self.playlist_data.get("tracks", [])),
            "played_tracks": [],
            "current_track": None,
            "liked_tracks": [],
            "disliked_tracks": [],
            "seed_tags": self.playlist_data.get("seed_tags", []),
            "user_id": self.user_id,
            "voice_channel_id": voice_channel.id,
            "text_channel_id": channel_id,
            "use_dj": use_dj,
            "candidate_pool": [{"track": t, "base_score": 50, "age": 0} for t in (self.playlist_data.get("candidate_pool_seeds") or [])],
        }
        set_radio_state(guild_id, state_dict)

        # Persist to database session state
        from bot.audio import persist_radio_state_helper
        asyncio.create_task(
            persist_radio_state_helper(guild_id, self.session_service, channel_id, state_dict)
        )


        audio_queue = asyncio.Queue(maxsize=3)
        task1 = asyncio.create_task(
            audio_player_task(vc, audio_queue, voice_channel, self.abort_event)
        )
        task2 = asyncio.create_task(
            build_radio_sequence(
                audio_queue, use_dj, guild_id,
                self.session_service, None,  # artifact_service passed as None, audio.py creates its own
                channel_id, self.abort_event,
            )
        )

        # Prevent GC
        for task in (task1, task2):
            task.add_done_callback(lambda t: None)


# ---------------------------------------------------------------------------
# Skip / Stop View (for audio playback)
# ---------------------------------------------------------------------------

class SkipView(discord.ui.View):
    def __init__(self, vc, queue, abort_event, state=None):
        super().__init__(timeout=None)
        self.vc = vc
        self.queue = queue
        self.abort_event = abort_event
        self.state = state
        self._add_queue_dropdowns()

    def _add_queue_dropdowns(self):
        if not self.state: return
        entries = self.state.get("display_queue", []) + self.state.get("upcoming_tracks", [])
        visible = entries[:25]
        if not visible: return

        # "Remove Track" Dropdown
        remove_options = [
            discord.SelectOption(
                label=f"{i+1}. {t.get('title')[:80]}",
                description=t.get("artist")[:100],
                value=str(i)
            ) for i, t in enumerate(visible)
        ]
        
        self.remove_select = discord.ui.Select(
            placeholder="❌ Remove Track...",
            options=remove_options,
            row=2,
            custom_id="remove_track_select"
        )
        self.remove_select.callback = self.remove_track_callback
        self.add_item(self.remove_select)

        # "Bump to Next" Dropdown
        bump_options = [
            discord.SelectOption(
                label=f"{i+1}. {t.get('title')[:80]}",
                description=t.get("artist")[:100],
                value=str(i)
            ) for i, t in enumerate(visible)
        ]
        self.bump_select = discord.ui.Select(
            placeholder="⬆️ Play Next...",
            options=bump_options,
            row=3,
            custom_id="bump_track_select"
        )
        self.bump_select.callback = self.bump_track_callback
        self.add_item(self.bump_select)

    async def remove_track_callback(self, interaction: discord.Interaction):
        await self._ack(interaction)
        idx = int(self.remove_select.values[0])
        dq_len = len(self.state.get("display_queue", []))
        if idx < dq_len:
            await self._reply(interaction, "Cannot remove this track; it is already loading or buffered.")
            return
            
        removed = self.state.get("upcoming_tracks", []).pop(idx - dq_len)
        await self._reply(interaction, f"Removed **{removed.get('title')}** from the queue.")
        
        from bot.audio import _render_queue_card
        content = _render_queue_card(self.state)
        if hasattr(self, 'remove_select'): self.remove_item(self.remove_select)
        if hasattr(self, 'bump_select'): self.remove_item(self.bump_select)
        self._add_queue_dropdowns()
        await interaction.message.edit(content=content, view=self)

    async def bump_track_callback(self, interaction: discord.Interaction):
        await self._ack(interaction)
        idx = int(self.bump_select.values[0])
        dq_len = len(self.state.get("display_queue", []))
        if idx < dq_len:
            await self._reply(interaction, "This track is already loading or playing next.")
            return
            
        bumped = self.state.get("upcoming_tracks", []).pop(idx - dq_len)
        self.state.get("upcoming_tracks", []).insert(0, bumped)
        await self._reply(interaction, f"Bumped **{bumped.get('title')}** to play next.")
        
        from bot.audio import _render_queue_card
        content = _render_queue_card(self.state)
        if hasattr(self, 'remove_select'): self.remove_item(self.remove_select)
        if hasattr(self, 'bump_select'): self.remove_item(self.bump_select)
        self._add_queue_dropdowns()
        await interaction.message.edit(content=content, view=self)

    async def _ack(self, interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            return True
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("Could not acknowledge control interaction: %s", e)
            return False

    async def _reply(self, interaction, message):
        try:
            await interaction.followup.send(message, ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("Could not send control reply: %s", e)

    @discord.ui.button(label="Like", style=discord.ButtonStyle.success, emoji="👍")
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        acknowledged = await self._ack(interaction)
        if not acknowledged:
            return

        guild_id = interaction.guild.id
        from app.radio_state import active_radios, now_playing_cache
        state = active_radios.get(guild_id)
        if not state or not state.get("active"):
            await self._reply(interaction, "No active radio station.")
            return

        current = now_playing_cache.get(guild_id, state.get("current_track"))
        if not current:
            await self._reply(interaction, "No song currently playing.")
            return

        # Scrobble to YTM history only — deliberately does NOT affect JIT scoring
        async def _scrobble_like(song_label: str, guild_id: int):
            try:
                from app.ytmusic_tools import search_ytmusic_track
                from app.auth import get_ytm_client
                import asyncio as _asyncio
                host_id = state.get("user_id")
                user_yt = get_ytm_client(host_id)
                if user_yt:
                    track = await search_ytmusic_track(song_label)
                    if track and track.get("videoId"):
                        song_data = await _asyncio.to_thread(user_yt.get_song, track["videoId"])
                        await _asyncio.to_thread(user_yt.add_history_item, song_data)
                        logger.info("Like-scrobbled '%s' to YTM history for host %s", song_label, host_id)
            except Exception as e:
                logger.warning("Failed to like-scrobble %s: %s", song_label, e)

        import asyncio
        asyncio.create_task(_scrobble_like(current, guild_id))
        await self._reply(interaction, f"👍 Liked '{current}' — scrobbled to your YTM history!")

    @discord.ui.button(label="Dislike / Skip", style=discord.ButtonStyle.danger, emoji="👎")
    async def dislike_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        acknowledged = await self._ack(interaction)
        if not acknowledged:
            return

        guild_id = interaction.guild.id
        from app.radio_state import active_radios, now_playing_cache
        state = active_radios.get(guild_id)
        if not state or not state.get("active"):
            await self._reply(interaction, "No active radio station.")
            return

        current = now_playing_cache.get(guild_id, state.get("current_track"))
        if not current:
            await self._reply(interaction, "No song currently playing.")
            return

        parts = current.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip() if len(parts) > 1 else ""

        disliked = state.setdefault("disliked_tracks", [])

        # Add to disliked if not already present
        if not any(t.get("artist") == artist and t.get("title") == title for t in disliked):
            disliked.append({"artist": artist, "title": title})
            
        # Skip the track
        if self.vc and self.vc.is_playing():
            self.vc.stop()

        await self._reply(interaction, f"👎 Disliked & Skipped '{current}'. Future tracks by this artist or similar will be avoided.")

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger, emoji="\u23ed\ufe0f")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        acknowledged = await self._ack(interaction)
        if self.vc and self.vc.is_playing():
            self.vc.stop()
        if acknowledged:
            await self._reply(interaction, "\u23ed\ufe0f Skipped to the next item.")

    @discord.ui.button(label="Stop Station", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        acknowledged = await self._ack(interaction)
        self.abort_event.set()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Exception:
                break
        await self.queue.put(None)
        if self.vc and self.vc.is_playing():
            self.vc.stop()
        if acknowledged:
            await self._reply(interaction, "\u23f9\ufe0f Station stopped.")

    @discord.ui.button(label="⚙️", style=discord.ButtonStyle.secondary)
    async def settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        view = RadioSettingsView(guild_id)
        await interaction.response.send_message("Radio Settings", view=view, ephemeral=True)


class RadioSettingsView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        
        from app.radio_state import active_radios
        state = active_radios.get(self.guild_id, {})
        jit_enabled = state.get("jit_enabled", True)
        current_mode = state.get("mode", "standard")
        
        self.jit_btn = discord.ui.Button(
            label="JIT Auto-Gen: " + ("ON" if jit_enabled else "OFF"), 
            style=discord.ButtonStyle.success if jit_enabled else discord.ButtonStyle.secondary,
            custom_id="toggle_jit",
            row=0
        )
        self.jit_btn.callback = self.toggle_jit
        self.add_item(self.jit_btn)
        
        # Add Select for Radio Mode
        options = [
            discord.SelectOption(label="Standard Mode", value="standard", description="Hybrid mix (Drift + Thesis)", default=(current_mode=="standard")),
            discord.SelectOption(label="YTM Native Radio", value="ytm_native", description="Organic discovery (YTM Algorithm)", default=(current_mode=="ytm_native")),
            discord.SelectOption(label="Strict Thesis", value="strict_thesis", description="Strictly adhere to the prompt", default=(current_mode=="strict_thesis")),
        ]
        self.mode_select = discord.ui.Select(
            placeholder="Choose Radio Mode...",
            min_values=1,
            max_values=1,
            options=options,
            row=1
        )
        self.mode_select.callback = self.change_mode
        self.add_item(self.mode_select)

    async def change_mode(self, interaction: discord.Interaction):
        from app.radio_state import active_radios
        state = active_radios.get(self.guild_id)
        if not state:
            await interaction.response.send_message("No active radio.", ephemeral=True)
            return
            
        new_mode = self.mode_select.values[0]
        state["mode"] = new_mode
        
        for opt in self.mode_select.options:
            opt.default = (opt.value == new_mode)
            
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"Radio mode changed to '{new_mode}'. (Takes effect next JIT replenishment)", ephemeral=True)

    async def toggle_jit(self, interaction: discord.Interaction):
        from app.radio_state import active_radios
        state = active_radios.get(self.guild_id)
        if not state:
            await interaction.response.send_message("No active radio.", ephemeral=True)
            return
            
        current = state.get("jit_enabled", True)
        new_val = not current
        state["jit_enabled"] = new_val
        
        extra_msg = ""
        if new_val:
            upcoming = state.get("upcoming_tracks", [])
            if len(upcoming) > 4:
                to_pool = upcoming[4:]
                state["upcoming_tracks"] = upcoming[:4]
                pool = state.setdefault("candidate_pool", [])
                for t in to_pool:
                    pool.append({"track": t, "base_score": 50, "age": 0})
                extra_msg = f" Moved {len(to_pool)} tracks to the candidate pool to allow JIT to steer."
        
        self.jit_btn.label = "JIT Auto-Gen: " + ("ON" if new_val else "OFF")
        self.jit_btn.style = discord.ButtonStyle.success if new_val else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"JIT Auto-Generation is now {'ON' if new_val else 'OFF'}.{extra_msg}", ephemeral=True)



class AdventureView(discord.ui.View):
    """View containing interactive buttons for choices presented during an adventure."""

    def __init__(
        self,
        choices: list[str],
        user_id: str,
        session_id: str,
        runner,
        artifact_service,
        session_service,
        update_state_fn,
        process_adventure_turn_fn,
    ):
        super().__init__(timeout=86400.0)
        self.user_id = user_id
        self.session_id = session_id
        self.runner = runner
        self.artifact_service = artifact_service
        self.session_service = session_service
        self.update_state_fn = update_state_fn
        self.process_adventure_turn_fn = process_adventure_turn_fn

        # Add a button for each choice (up to 5 buttons in a single row)
        for choice in choices[:5]:
            button = discord.ui.Button(label=choice[:80], style=discord.ButtonStyle.secondary)
            button.callback = self.make_callback(choice)
            self.add_item(button)

    def make_callback(self, choice: str):
        async def callback(interaction: discord.Interaction):
            # Defer interaction to show a loading state
            await interaction.response.defer(thinking=True)

            try:
                # Log choice to thread chat so everyone can see the decision
                await interaction.channel.send(content=f"**Action:** *{choice}*")
            except Exception as e:
                logger.error("Failed to send action message to channel: %s", e)

            # Trigger the adventure turn processing
            try:
                await self.process_adventure_turn_fn(
                    channel=interaction.channel,
                    author=interaction.user,
                    content=choice,
                    user_id=self.user_id,
                    session_id=self.session_id,
                    interaction=interaction,
                )
            except Exception as e:
                logger.exception("Failed running adventure turn from button:")
                try:
                    await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)
                except Exception:
                    pass
        return callback

