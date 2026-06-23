# Sophee Agent

A multi-agent Discord bot built on the [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/). Sophee is an internet radio DJ, art director, music scholar, news researcher, and general-purpose assistant — all coordinated by an LLM router that interprets natural language and delegates to the right specialist.

No slash commands. Just @ mention her and talk.

---

## Features

### 🎙️ Internet Radio DJ
- **Playlist Generation** — describe a vibe, genre, or theme and Sophee curates a validated 4-track starting sequence using agentic weighted tag expansion + Last.fm validation
- **Live Voice Playback** — connects to Discord voice channels, downloads songs via yt-dlp, and streams audio with volume control
- **Track Feedback & Scoring** — thumbs up/down (👍/👎) for session-based JIT steering, and persistent Heart (💖) button to save favorites globally
- **Auto-Replenishment** — queue dynamically JIT-refills to maintain exactly 3 upcoming tracks, scoring candidates on the fly using your feedback
- **Discovery Modes** — supports Standard, Genre Discovery (using seed tags and favorites negatively to block duplicates), and Favorites Discovery (seeds from your persistent favorites profile)
- **DJ Commentary** — generates spoken voice segments between tracks (segues, trivia, station IDs, fake sponsor reads, mood checks, listener mail, field reports) using Google TTS
- **Time-Aware DJ** — commentary references the current time of day and day of week naturally
- **Queue Management** — show upcoming tracks, add/insert tracks (with play-next support), remove, shuffle, and steer the radio direction mid-broadcast
- **Playlist Mutation** — reroll playlists through Last.fm similarity (smooth or chaotic mutation modes)
- **New Music Discovery** — fetches actual new releases from MusicBrainz (structured data, no hallucination), falls back to Gemini Search Grounding for niche genres
- **Song Cache** — downloaded songs are cached locally with LRU eviction at 500MB

### 🎨 Art Director
- **Image Generation** — generates images via Google's Imagen through the Gemini API
- **Style Rolling** — randomly selects artists from a curated catalog across three dimensions (medium/line, lighting/atmosphere, genre/subject) and blends them into prompts
- **Image Editing** — edit, reroll (same prompt new seed), and restyle (same prompt new artist style) via Discord button UI
- **Art Scholar Mode** — ask for style inspiration and Sophee introduces the rolled artists with biographical context, researching unfamiliar artists via Google Search
- **Metadata Tracking** — image prompts, styles, and resolutions are cached per-message for accurate edit/reroll chains

### 🎵 Music Expert
- **Deep Music Analysis** — album lore, lyrical analysis, production breakdowns, artist histories, genre context
- **Live Data Enrichment** — uses Last.fm primitives to look up real artist bios, track tags, play counts, and similar artists instead of relying solely on training data
- **Trending Awareness** — can pull this week's global or regional charts from Last.fm

### 📰 Researcher
- **News & Current Events** — uses Google Search Grounding + Google News RSS to research topics and synthesize narrative essays (not bullet-point dumps)
- **Fact-Checking** — grounded in live search results, not training data

### 💬 General Assistant
- **Conversational AI** — clean, unbiased, direct responses for general Q&A, coding, writing, math, chitchat

### 🧠 Per-User Learning
- **Behavioral Memory** — users can correct Sophee's behavior and she remembers per Discord user ID
- **Persistent Preferences** — stored in session state (SQLite), survives bot restarts
- **Capped at 20 corrections** per user (oldest roll off to prevent context bloat)
- **Injected into prompts** — preferences are prepended to every message automatically

### ⚡ Infrastructure
- **Persistent Sessions** — SQLite via ADK's `DatabaseSessionService`, all state survives restarts
- **History Trimming** — conversation history capped at 40 turns to control token costs
- **Per-User Rate Limiting** — 3-second cooldown per user to prevent spam
- **Multi-Agent Router** — root agent analyzes every message and instantly delegates to the right specialist (no wasted tokens on the wrong agent)
- **Structured Logging** — Python `logging` throughout, no print statements
- **Async Throughout** — asyncio locks, async Last.fm/MusicBrainz calls, non-blocking yt-dlp downloads

---

## Architecture

```
sophee-agent/
├── app/                        # Agent logic (no Discord dependency)
│   ├── agent.py                # Agent definitions, prompt loading, tool wiring
│   ├── tools.py                # Core tools (image, TTS, news, Last.fm, MusicBrainz)
│   ├── radio_tools.py          # Queue management tools (show, add, remove, shuffle, steer)
│   ├── radio_state.py          # Shared radio state registry (guild-keyed)
│   ├── user_tools.py           # Per-user preference tools (remember, get, clear)
│   ├── fast_api_app.py         # FastAPI server for ADK web UI
│   ├── artists_catalog.json    # Curated artist catalog for style rolling
│   ├── app_utils/
│   │   ├── telemetry.py        # OpenTelemetry config
│   │   └── typing.py           # Pydantic models
│   └── prompts/                # System prompts (editable .md files)
│       ├── root_agent.md
│       ├── dj_agent.md
│       ├── art_director.md
│       ├── music_expert.md
│       ├── researcher.md
│       └── general_assistant.md
├── bot/                        # Discord interface
│   ├── client.py               # Event handling, session management, message routing
│   ├── views.py                # Discord UI (ImageView, RadioView, SkipView, modals)
│   ├── audio.py                # Audio playback engine, DJ segments, song downloading
│   ├── cache.py                # Image metadata cache (async, JSON-persisted)
│   ├── message_utils.py        # Message chunking, URL bracketing, context fetching
│   ├── rate_limiter.py         # Per-user cooldown
│   └── history.py              # Session history trimming
├── scripts/
│   └── clear_slash_commands.py # Utility to remove stale Discord slash commands
├── pyproject.toml
├── Dockerfile
└── .env.example
```

### Agent Routing

```
User message → Root Agent (router)
                 ├── dj_agent          (music playback, playlists, queue management)
                 ├── art_director      (image generation, style inspiration)
                 ├── music_expert      (music analysis, lore, scholarship)
                 ├── researcher        (news, current events, fact-checking)
                 └── general_assistant (everything else)
```

The root agent never responds directly — it immediately transfers to the most appropriate specialist.

---

## Last.fm Integration

### Hardcoded Pipelines (reliable, deterministic)
| Function | Purpose |
|---|---|
| `generate_radio_playlist` | Full pipeline: LLM curation → Last.fm validation → state registration |
| `steer_radio` | Clears queue, refills from tag/similar/new releases |
| `fetch_new_music_releases` | MusicBrainz → Last.fm track lookup (Gemini fallback) |

### Agent Primitives (LLM composes these freestyle)
| Tool | Purpose |
|---|---|
| `search_lastfm` | General search for tracks and artists |
| `get_artist_info` | Bio, tags, similar artists, play counts |
| `get_track_info` | Tags, album, wiki, play count for a specific track |
| `get_trending_tracks` | Global or by-country weekly charts |
| `get_trending_artists` | Global or by-country trending artists |

---

## Setup

### Prerequisites
- Python 3.11+
- FFmpeg (for voice playback)
- A Discord bot token
- A Gemini API key
- A Last.fm API key

### Installation

```bash
git clone https://github.com/youruser/sophee-agent.git
cd sophee-agent
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate

pip install -e .
```

### Configuration

Copy `.env.example` to `.env` and fill in your keys:

```env
DISCORD_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_api_key
LASTFM_KEY=your_lastfm_api_key
```

### Running

```bash
python bot/client.py
```

---

## API Keys

| Service | Required | Free? | Notes |
|---|---|---|---|
| Discord | Yes | Yes | [Discord Developer Portal](https://discord.com/developers) |
| Gemini | Yes | Yes (free tier) | [Google AI Studio](https://aistudio.google.com/) |
| Last.fm | Yes | Yes | [Last.fm API](https://www.last.fm/api/account/create) |
| MusicBrainz | No (automatic) | Yes | No key needed, just a User-Agent header |

---

## License

MIT
