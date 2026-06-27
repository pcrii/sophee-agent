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
# Shared helper: run agent and extract image artifact
# ---------------------------------------------------------------------------

async def _run_agent_and_get_image(
    runner, artifact_service, user_id, session_id, prompt_text
):
    """Runs the agent with a prompt and returns (temp_file_path, response_text, new_image_key) or (None, response_text, None)."""
    before_keys = set(
        await artifact_service.list_artifact_keys(
            app_name="app", user_id=user_id, session_id=session_id
        )
    )

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

class ImageEditModal(discord.ui.Modal, title="Edit Image"):
    prompt_input = discord.ui.TextInput(
        label="New Prompt / Edit Instructions",
        style=discord.TextStyle.paragraph,
        placeholder="e.g., make it darker, add a hat...",
        required=True,
    )

    def __init__(self, user_id, session_id, runner, artifact_service, session_service, update_state_fn):
        super().__init__()
        self.user_id = user_id
        self.session_id = session_id
        self.runner = runner
        self.artifact_service = artifact_service
        self.session_service = session_service
        self.update_state_fn = update_state_fn

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            parent_msg_id = str(interaction.message.id) if interaction.message else None
            ref_meta = await get_image_metadata(parent_msg_id)
            original_prompt = ""
            state_updates = {
                "latest_input_image": None,  # Clear by default
                "latest_input_image_artifact": None,
            }
            active_session_id = self.session_id
            if ref_meta:
                original_prompt = ref_meta.get("prompt", "")
                if ref_meta.get("session_id"):
                    active_session_id = ref_meta.get("session_id")
                    logger.info("Routing edit modal submission to isolated session: %s", active_session_id)
                state_updates.update({
                    "rolled_style": ref_meta.get("style"),
                    "latest_resolution": ref_meta.get("resolution"),
                })
                
                image_artifact = ref_meta.get("image_artifact")
                if image_artifact:
                    parent_artifact = ref_meta.get("parent_image_artifact")
                    # If edit prompt requests original composition, load parent artifact
                    prompt_lower = self.prompt_input.value.lower()
                    if parent_artifact and any(kw in prompt_lower for kw in ["original", "source", "first", "initial", "seed"]):
                        logger.info("User requested original composition in edit modal. Routing to parent artifact: %s", parent_artifact)
                        image_artifact = parent_artifact
                    try:
                        part = await self.artifact_service.load_artifact(
                            app_name="app",
                            user_id=self.user_id,
                            filename=image_artifact,
                            session_id=active_session_id,
                        )
                        if part and part.inline_data and part.inline_data.data:
                            img_b64 = base64.b64encode(part.inline_data.data).decode("utf-8")
                            state_updates["latest_input_image"] = {
                                "data": img_b64,
                                "mime_type": part.inline_data.mime_type or "image/jpeg",
                                "original_prompt": original_prompt or "Generate an image",
                            }
                            state_updates["latest_input_image_artifact"] = image_artifact
                            logger.info("Loaded reference image for edit from artifact: %s", image_artifact)
                    except Exception as e:
                        logger.error("Failed to load reference image artifact %s for edit: %s", image_artifact, e)

            await self.update_state_fn(self.user_id, active_session_id, state_updates)

            edit_prompt = f"Image edit request: {self.prompt_input.value}"

            temp_path, response_text, new_image_key = await _run_agent_and_get_image(
                self.runner, self.artifact_service, self.user_id, active_session_id, edit_prompt
            )

            if temp_path:
                view = ImageView(
                    self.user_id, active_session_id,
                    self.runner, self.artifact_service, self.session_service, self.update_state_fn,
                )
                session = await self.session_service.get_session(
                    app_name="app", user_id=self.user_id, session_id=active_session_id
                )
                new_prompt = f"{original_prompt} -> edited to: {self.prompt_input.value}" if original_prompt else self.prompt_input.value

                thread_content = f"Edited based on: **{self.prompt_input.value}**\n\n{response_text}" if response_text else f"Edited based on: **{self.prompt_input.value}**"
                sent_msg = await _send_image_with_thread(
                    interaction, temp_path, thread_content, view,
                )

                last_prompt = session.state.get("last_generated_prompt") if session else None
                await save_image_metadata(
                    message_id=str(sent_msg.id),
                    prompt=last_prompt or new_prompt,
                    style=session.state.get("rolled_style") if session else None,
                    resolution=session.state.get("latest_resolution", "0.5k") if session else "0.5k",
                    image_artifact=new_image_key,
                    parent_image_artifact=session.state.get("latest_input_image_artifact") if session else None,
                    session_id=active_session_id,
                )
            else:
                await interaction.followup.send(response_text or "Failed to generate edited image.")
        except Exception as e:
            logger.error("Image editing error: %s", e)
            await interaction.followup.send(f"Error editing image: {e}")


# ---------------------------------------------------------------------------
# Image View (Edit / Reroll / Restyle buttons)
# ---------------------------------------------------------------------------

class ImageView(discord.ui.View):
    def __init__(self, user_id, session_id, runner, artifact_service, session_service, update_state_fn):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        self.runner = runner
        self.artifact_service = artifact_service
        self.session_service = session_service
        self.update_state_fn = update_state_fn

    @discord.ui.button(label="Edit Image", style=discord.ButtonStyle.primary, emoji="\u270f\ufe0f")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = ImageEditModal(
            self.user_id, self.session_id,
            self.runner, self.artifact_service, self.session_service, self.update_state_fn,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Reroll", style=discord.ButtonStyle.secondary, emoji="\U0001f501")
    async def reroll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            parent_msg_id = str(interaction.message.id)
            ref_meta = await get_image_metadata(parent_msg_id)

            original_prompt = ""
            active_session_id = self.session_id
            if ref_meta:
                original_prompt = ref_meta.get("prompt", "")
                if ref_meta.get("session_id"):
                    active_session_id = ref_meta.get("session_id")
                await self.update_state_fn(self.user_id, active_session_id, {
                    "rolled_style": ref_meta.get("style"),
                    "latest_resolution": ref_meta.get("resolution"),
                    "force_style_roll": False,
                    "start_fresh_image": True,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                })
            else:
                await self.update_state_fn(self.user_id, active_session_id, {
                    "force_style_roll": False,
                    "start_fresh_image": True,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                })

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

    @discord.ui.button(label="Restyle", style=discord.ButtonStyle.secondary, emoji="\U0001f58c\ufe0f")
    async def restyle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            parent_msg_id = str(interaction.message.id)
            ref_meta = await get_image_metadata(parent_msg_id)

            original_prompt = ""
            active_session_id = self.session_id
            if ref_meta:
                original_prompt = ref_meta.get("prompt", "")
                if ref_meta.get("session_id"):
                    active_session_id = ref_meta.get("session_id")
                await self.update_state_fn(self.user_id, active_session_id, {
                    "rolled_style": ref_meta.get("style"),
                    "latest_resolution": ref_meta.get("resolution"),
                    "force_style_roll": True,
                    "art_director_mode": "simple",
                    "start_fresh_image": False,
                    "latest_input_image": None,
                    "latest_input_image_artifact": None,
                })
            else:
                await self.update_state_fn(self.user_id, active_session_id, {
                    "force_style_roll": True, "art_director_mode": "simple", "start_fresh_image": False, "latest_input_image": None, "latest_input_image_artifact": None,
                })

            if original_prompt:
                run_prompt = f"Roll a random artist inspiration style and apply it to the prompt: '{original_prompt}'."
            else:
                run_prompt = "Roll a random artist inspiration style and apply it to the previous prompt."

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
                    interaction, temp_path, f"\U0001f58c\ufe0f **Restyle**\n\n{response_text}", view,
                )

                session = await self.session_service.get_session(
                    app_name="app", user_id=self.user_id, session_id=active_session_id
                )
                last_prompt = session.state.get("last_generated_prompt") if session else None
                await save_image_metadata(
                    message_id=str(sent_msg.id),
                    prompt=last_prompt or original_prompt or "restyled image",
                    style=session.state.get("rolled_style") if session else None,
                    resolution=session.state.get("latest_resolution", "0.5k") if session else "0.5k",
                    image_artifact=new_image_key,
                    session_id=active_session_id,
                )
            else:
                await interaction.followup.send(response_text or "Failed to restyle image.")
        except Exception as e:
            logger.error("Error restyling image: %s", e)
            await interaction.followup.send(f"Error restyling image: {e}")


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
        state_dict = {
            "active": True,
            "playlist_thesis": self.playlist_data.get("playlist_thesis", "music"),
            "genre": self.playlist_data.get("playlist_thesis", "music"),
            "upcoming_tracks": list(self.playlist_data.get("tracks", [])),
            "played_tracks": [],
            "current_track": None,
            "liked_tracks": [],
            "disliked_tracks": [],
            "mode": self.playlist_data.get("mode", "standard"),
            "seed_tags": self.playlist_data.get("seed_tags", []),
            "user_id": self.user_id,
            "voice_channel_id": voice_channel.id,
            "text_channel_id": channel_id,
            "use_dj": use_dj,
        }
        set_radio_state(guild_id, state_dict)

        # Persist to database session state
        from bot.audio import persist_radio_state_helper
        asyncio.create_task(
            persist_radio_state_helper(guild_id, self.session_service, channel_id, state_dict)
        )


        audio_queue = asyncio.Queue()
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
    def __init__(self, vc, queue, abort_event):
        super().__init__(timeout=None)
        self.vc = vc
        self.queue = queue
        self.abort_event = abort_event

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
            discord.SelectOption(label="Standard Mode", value="standard", description="Default mix", default=(current_mode=="standard")),
            discord.SelectOption(label="Discovery Genre", value="discovery_genre", description="Explore specific genres", default=(current_mode=="discovery_genre")),
            discord.SelectOption(label="Discovery Favorites", value="discovery_favorites", description="Explore based on likes", default=(current_mode=="discovery_favorites")),
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

