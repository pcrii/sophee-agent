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

from app.tools import (
    fetch_google_news,
    generate_image,
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
from app.radio_tools import (
    add_to_queue,
    change_radio_mode,
    mutate_upcoming_queue,
    remove_from_queue,
    show_station_queue,
    shuffle_queue,
    steer_radio,
    stop_station,
)
from app.user_tools import (
    clear_preferences,
    get_user_profile,
    remember_preference,
)
from app.suggestion_box import (
    scrape_suggestion_box,
    read_suggestion_box,
)
from app.adventure_tools import (
    start_adventure,
    update_adventure_state,
    end_adventure,
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
# Model configuration
# NOTE: DO NOT use the Gemini Interactions API (use_interactions_api=True)
# here or in any helper tools. The Interactions API currently breaks
# automatic function calling (Python), which ADK relies on for agent tools.
# Always use the standard generate_content API (the default).
# ---------------------------------------------------------------------------

model_config = Gemini(
    model="gemini-3.1-flash-lite",
    retry_options=types.HttpRetryOptions(attempts=3),
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
_user_tools = [remember_preference, get_user_profile, clear_preferences]


# ---------------------------------------------------------------------------
# Sub-Agents
# ---------------------------------------------------------------------------

dj_agent = Agent(
    name="dj_agent",
    model=model_config,
    description="An expert radio DJ and music enthusiast who curates playlists, plays music, and generates voice DJ commentary.",
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
        mutate_upcoming_queue,
        stop_station,
        # Last.fm primitives — agent can freestyle with these
        search_lastfm,
        get_artist_info,
        get_track_info,
        get_trending_tracks,
        get_trending_artists,
        *_user_tools,
    ],
)

art_director = Agent(
    name="art_director",
    model=model_config,
    description="A professional illustrator and art scholar who designs drawings/images and teaches the user about art styles.",
    instruction=_load_prompt("art_director"),
    tools=[
        generate_image,
        roll_artistic_inspiration,
        get_art_director_settings,
        custom_google_search,
        *_user_tools,
    ],
)

music_expert = Agent(
    name="music_expert",
    model=model_config,
    description="A highly passionate and enthusiastic music scholar/critic who explores song meanings, album lore, narrative building, and artist intentions.",
    instruction=_load_prompt("music_expert"),
    tools=[
        get_now_playing,
        # Last.fm primitives for music research
        search_lastfm,
        get_artist_info,
        get_track_info,
        get_trending_tracks,
        get_trending_artists,
        *_user_tools,
    ],
)

researcher = Agent(
    name="researcher",
    model=model_config,
    description="A news researcher and investigator who finds information on current events, compiles summaries, and provides narrative essays on recent news topics.",
    instruction=_load_prompt("researcher"),
    tools=[
        custom_google_search,
        fetch_google_news,
        generate_tts,
        generate_tts_script,
        get_now_playing,
        *_user_tools,
    ],
)

general_assistant = Agent(
    name="general_assistant",
    model=model_config,
    description="A clean, helpful, and unbiased conversational assistant for general questions, chit-chat, Q&A, writing, and coding.",
    instruction=_load_prompt("general_assistant"),
    tools=[
        fetch_google_news,
        generate_tts,
        generate_tts_script,
        get_now_playing,
        # Suggestion box
        scrape_suggestion_box,
        read_suggestion_box,
        *_user_tools,
    ],
)

dm_agent = Agent(
    name="dm_agent",
    model=model_config,
    description="An interactive tabletop RPG Dungeon Master (DM) who runs narrative adventure sessions, guides players, and manages quest inventory/tension stats.",
    instruction=_load_prompt("dm_agent"),
    tools=[
        start_adventure,
        update_adventure_state,
        end_adventure,
        *_user_tools,
    ],
)

root_agent = Agent(
    name="root_agent",
    model=model_config,
    instruction=_load_prompt("root_agent"),
    sub_agents=[dj_agent, art_director, music_expert, researcher, general_assistant, dm_agent],
)


# ---------------------------------------------------------------------------
# ADK App
# ---------------------------------------------------------------------------

app = App(
    root_agent=root_agent,
    name="app",
)
