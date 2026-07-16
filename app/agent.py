"""Multi-agent architecture for Sophee using Google ADK.

Defines 5 specialized sub-agents under a root coordinator.
System prompts are loaded from app/prompts/*.md files.
"""

import os

from dotenv import load_dotenv

# Load workspace .env (parent directory) since it contains the API keys
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
load_dotenv()  # Fallback to local .env

# Configure ADK to use AI Studio instead of Vertex AI
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
os.environ["GOOGLE_API_KEY"] = os.getenv(
    "GEMINI_TOKEN", os.getenv("GEMINI_API_KEY", "")
)

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types
# from google.adk.tools.mcp_tool import McpToolset
# from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
# from mcp import StdioServerParameters

from app.tools import (
    get_pending_suggestions,
    mark_suggestion_status,
    search_conversation_history,
    fetch_google_news,
    stop_radio_station,
    start_radio_station,
    generate_tts,
    generate_tts_script,
    get_art_director_settings,
    get_artist_info,
    get_now_playing,
    get_track_info,
    get_trending_artists,
    get_trending_tracks,
    roll_artistic_inspiration,
    search_lastfm,
)
from app.image_tools import (
    gemini_generate_image,
    preprocess_image,
    show_image_settings,
    set_image_defaults,
)
from app.radio_tools import (
    add_to_queue,
    change_radio_mode,
    mutate_upcoming_queue,
    remove_from_queue,
    resume_radio,
    show_station_queue,
    shuffle_queue,
    steer_radio,
    stop_station,
    toggle_radio_jit,
    hibernate_radio,
    configure_radio_settings,
    open_radio_settings_menu,
)
from app.user_tools import (
    clear_preferences,
    delete_preference,
    get_user_profile,
    remember_preference,
)
from app.suggestion_box import (
    scrape_suggestion_box,
    read_suggestion_box,
)
from app.musicbrainz_tools import (
    search_musicbrainz_artist,
    get_musicbrainz_artist_releases,
    get_musicbrainz_artist_relationships,
)
from app.ytmusic_tools import (
    search_ytmusic_track,
    search_ytmusic_artist,
    generate_ytmusic_radio,
    get_ytmusic_similar_artists,
    get_ytmusic_charts,
    get_ytmusic_mood_playlists,
    load_ytmusic_playlist,
    search_ytmusic_library_playlists,
)


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """Loads a system prompt from app/prompts/{name}.md"""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", f"{name}.md")
    with open(prompt_path, encoding="utf-8") as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# Monkey Patch ADK Interactions API Tool Formatting
# (Fixes a bug in the ADK library where parameterless tools cause a 400 crash)
# ---------------------------------------------------------------------------
import google.adk.models.interactions_utils as _adk_int_utils

_orig_convert = _adk_int_utils.convert_tools_config_to_interactions_format

def _patched_convert(config):
    tools = _orig_convert(config)
    for t in tools:
        if t.get("type") == "function" and "parameters" not in t:
            t["parameters"] = {"type": "object", "properties": {}}
    return tools

_adk_int_utils.convert_tools_config_to_interactions_format = _patched_convert

_orig_convert_step = _adk_int_utils._convert_interaction_step_to_parts

def _patched_convert_step(step):
    # Fixes AttributeError: 'FunctionCallStep' object has no attribute 'signature'
    if type(step).__name__ == "FunctionCallStep":
        from google.genai import types
        step_id = getattr(step, "id", "")
        step_name = getattr(step, "name", "")
        step_args = getattr(step, "arguments", {})
        step_sig = getattr(step, "signature", None)
        thought_sig = _adk_int_utils._decode_base64_string(step_sig) if step_sig else None
        
        return [
            types.Part(
                function_call=types.FunctionCall(
                    id=step_id,
                    name=step_name,
                    args=step_args or {},
                ),
                thought_signature=thought_sig,
            )
        ]
    return _orig_convert_step(step)

_adk_int_utils._convert_interaction_step_to_parts = _patched_convert_step

# ---------------------------------------------------------------------------
# Model configuration
# Note: Enabled Interactions API to leverage server-side conversational 
# history instead of sending huge context blocks.
# ---------------------------------------------------------------------------

model_config = Gemini(
    model="gemini-3.1-flash-lite",
    retry_options=types.HttpRetryOptions(attempts=3),
    use_interactions_api=True,
)



# ---------------------------------------------------------------------------
# Custom Google Search tool (enables built-in search alongside function calling)
# ---------------------------------------------------------------------------

class CustomGoogleSearchTool(GoogleSearchTool):
    async def process_llm_request(self, *, tool_context, llm_request) -> None:
        await super().process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )
        if llm_request.config is None:
            llm_request.config = types.GenerateContentConfig()
        if llm_request.config.tool_config is None:
            llm_request.config.tool_config = types.ToolConfig()
        llm_request.config.tool_config.include_server_side_tool_invocations = True


custom_google_search = CustomGoogleSearchTool()

# User tools available to all agents
_user_tools = [remember_preference, get_user_profile, delete_preference, clear_preferences, search_conversation_history]

# MusicBrainz relational database tools
_musicbrainz_tools = [
    search_musicbrainz_artist,
    get_musicbrainz_artist_releases,
    get_musicbrainz_artist_relationships,
]

# YouTube Music recommendation algorithms and fuzzy search
_ytmusic_tools = [
    search_ytmusic_track,
    search_ytmusic_artist,
    generate_ytmusic_radio,
    get_ytmusic_similar_artists,
    get_ytmusic_charts,
    get_ytmusic_mood_playlists,
    load_ytmusic_playlist,
    search_ytmusic_library_playlists,
]

# TODO: ComfyUI MCP integration — configure once ComfyUI server is set up
# comfy_mcp = McpToolset(...)


# ---------------------------------------------------------------------------
# Sub-Agents
# ---------------------------------------------------------------------------

dj_agent = Agent(
    name="dj_agent",
    model=model_config,
    description="An expert radio DJ and passionate music scholar who curates playlists, analyzes lyrics, discusses album lore, and generates voice DJ commentary.",
    instruction=_load_prompt("dj_agent"),
    tools=[
        start_radio_station,
        get_now_playing,
        generate_tts,
        generate_tts_script,
        show_station_queue,
        remove_from_queue,
        add_to_queue,
        shuffle_queue,
        steer_radio,
        change_radio_mode,
        stop_radio_station,
        mutate_upcoming_queue,
        stop_station,
        hibernate_radio,
        resume_radio,
        toggle_radio_jit,
        configure_radio_settings,
        open_radio_settings_menu,
        # Last.fm primitives — agent can freestyle with these
        search_lastfm,
        get_artist_info,
        get_track_info,
        get_trending_tracks,
        get_trending_artists,
        *_musicbrainz_tools,
        *_ytmusic_tools,
        *_user_tools,
    ],
)

art_director = Agent(
    name="art_director",
    model=model_config,
    description="A professional illustrator and art scholar who designs drawings/images and teaches the user about art styles.",
    instruction=_load_prompt("art_director"),
    tools=[
        gemini_generate_image,
        preprocess_image,
        roll_artistic_inspiration,
        get_art_director_settings,
        show_image_settings,
        set_image_defaults,
        custom_google_search,
        *_user_tools,
    ],
)



async def apply_llm_settings_callback(*, callback_context, llm_request, **kwargs):
    state = callback_context.state
    temp = state.get("llm_temperature")
    top_p = state.get("llm_top_p")
    top_k = state.get("llm_top_k")
    pres_pen = state.get("llm_presence_penalty")
    freq_pen = state.get("llm_frequency_penalty")

    if any(x is not None for x in [temp, top_p, top_k, pres_pen, freq_pen]):
        if llm_request.config is None:
            llm_request.config = types.GenerateContentConfig()
        if temp is not None:
            llm_request.config.temperature = temp
        if top_p is not None:
            llm_request.config.top_p = top_p
        if top_k is not None:
            llm_request.config.top_k = top_k
        if pres_pen is not None:
            llm_request.config.presence_penalty = pres_pen
        if freq_pen is not None:
            llm_request.config.frequency_penalty = freq_pen

    # Save metadata so Discord can cache it on the message ID for debugging
    state["last_llm_metadata"] = {
        "agent_name": "general_assistant",
        "history_length": len(llm_request.contents) if llm_request.contents else 0,
        "history_roles": [c.role for c in llm_request.contents] if llm_request.contents else [],
        "config": {
            "temperature": getattr(llm_request.config, "temperature", None) if llm_request.config else None,
            "top_p": getattr(llm_request.config, "top_p", None) if llm_request.config else None,
            "top_k": getattr(llm_request.config, "top_k", None) if llm_request.config else None,
            "presence_penalty": getattr(llm_request.config, "presence_penalty", None) if llm_request.config else None,
            "frequency_penalty": getattr(llm_request.config, "frequency_penalty", None) if llm_request.config else None,
        }
    }


general_assistant = Agent(
    name="general_assistant",
    model=model_config,
    description="A clean, helpful, and unbiased conversational assistant for general questions, chit-chat, Q&A, writing, and coding.",
    instruction=_load_prompt("general_assistant"),
    before_model_callback=apply_llm_settings_callback,
    tools=[
        custom_google_search,
        fetch_google_news,
        generate_tts,
        generate_tts_script,
        get_now_playing,
        # Suggestion box
        scrape_suggestion_box,
        read_suggestion_box,
        get_pending_suggestions,
        mark_suggestion_status,
        *_user_tools,
    ],
)

root_agent = Agent(
    name="root_agent",
    model=model_config,
    instruction=_load_prompt("root_agent") + "\n\nCRITICAL: Never execute sub-agent tools directly (e.g. gemini_generate_image, start_radio_station). You must ALWAYS use your transfer_to_* tools to pass the user's request to the appropriate sub-agent.",
    tools=dj_agent.tools + art_director.tools + general_assistant.tools,
    sub_agents=[dj_agent, art_director, general_assistant],
)


# ---------------------------------------------------------------------------
# ADK App
# ---------------------------------------------------------------------------

app = App(
    root_agent=root_agent,
    name="app",
)
