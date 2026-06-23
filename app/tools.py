"""Core tool functions for Sophee ADK agents.

Includes: news fetching, playlist generation with Last.fm validation,
TTS generation, image generation, art style rolling, and Last.fm helpers.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
import urllib.parse
import wave
import xml.etree.ElementTree as ET

import requests
from google import genai
from google.adk.tools import ToolContext
from google.genai import types

logger = logging.getLogger("sophee.app.tools")

# Cache directories
os.makedirs("song_cache", exist_ok=True)
os.makedirs("tts_cache", exist_ok=True)

LASTFM_KEY = os.getenv("LASTFM_KEY")


# ---------------------------------------------------------------------------
# Shared Utilities
# ---------------------------------------------------------------------------

def _pcm_to_wav(audio_bytes: bytes) -> bytes:
    """Wraps raw PCM audio data in a WAV header (16-bit, 24kHz, mono)."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        temp_path = f.name

    with wave.open(temp_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit PCM
        wav_file.setframerate(24000)
        wav_file.writeframes(audio_bytes)

    with open(temp_path, "rb") as wav_in:
        wav_data = wav_in.read()

    os.remove(temp_path)
    return wav_data


def _extract_audio_bytes(response) -> bytes:
    """Extracts raw audio bytes from a Gemini TTS response."""
    audio_bytes = b""
    if response.candidates and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                audio_bytes += part.inline_data.data
    return audio_bytes


def _extract_json(text: str) -> dict:
    """Strips markdown fencing and parses JSON."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
    elif text.startswith("```"):
        text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text.strip())


def is_duplicate(track: dict, track_list: list) -> bool:
    """Checks if a track already exists in a list (case-insensitive)."""
    artist = str(track.get("artist", "")).strip().lower()
    title = str(track.get("title", "")).strip().lower()
    for existing in track_list:
        e_artist = str(existing.get("artist", "")).strip().lower()
        e_title = str(existing.get("title", "")).strip().lower()
        if artist == e_artist and title == e_title:
            return True
    return False


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

async def fetch_google_news(topic: str) -> dict:
    """Fetches the top news articles from Google News search for a given topic or query.

    Args:
        topic: The search term or topic to look up (e.g., 'technology', 'Malheur County', 'indie music').

    Returns:
        A dictionary containing the list of top news article titles and their links.
    """
    encoded_topic = urllib.parse.quote(topic)
    url = f"https://news.google.com/rss/search?q={encoded_topic}&hl=en-US&gl=US&ceid=US:en"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        if response.status_code != 200:
            return {
                "status": "error",
                "message": f"Received status code {response.status_code} from Google News.",
            }

        try:
            root = ET.fromstring(response.content)
            items = root.findall(".//item")[:10]
            if not items:
                return {
                    "status": "success",
                    "articles": [],
                    "message": f"No articles found for search query '{topic}'.",
                }

            articles = []
            for i, item in enumerate(items):
                title_elem = item.find("title")
                link_elem = item.find("link")
                pub_date_elem = item.find("pubDate")

                articles.append({
                    "index": i + 1,
                    "title": title_elem.text if title_elem is not None else "Unknown Title",
                    "url": link_elem.text if link_elem is not None else "",
                    "published": pub_date_elem.text if pub_date_elem is not None else "",
                })

            return {"status": "success", "articles": articles}
        except ET.ParseError:
            return {"status": "error", "message": "Error parsing XML from Google News."}
    except Exception as e:
        return {"status": "error", "message": f"Exception fetching news: {e}"}


# ---------------------------------------------------------------------------
# Last.fm Validation & Helpers
# ---------------------------------------------------------------------------

async def validate_tracklist_via_lastfm(tracks: list) -> tuple:
    """Validates tracks against Last.fm — rejects tracks with <1000 listeners or not found."""
    valid_tracks = []
    invalid_tracks = []

    for track in tracks:
        artist = track.get("artist", "")
        title = track.get("title", "")
        if not artist or not title:
            invalid_tracks.append(track)
            continue

        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo"
            f"&api_key={LASTFM_KEY}"
            f"&artist={urllib.parse.quote(artist)}"
            f"&track={urllib.parse.quote(title)}"
            f"&format=json"
        )
        try:
            response = await asyncio.to_thread(requests.get, url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if "error" in data:
                    invalid_tracks.append(track)
                else:
                    listeners = int(data.get("track", {}).get("listeners", 0))
                    if listeners < 1000:
                        invalid_tracks.append(track)
                    else:
                        valid_tracks.append(track)
            else:
                # API error — optimistically accept
                valid_tracks.append(track)
        except Exception as e:
            logger.warning("Validation error for %s - %s: %s", artist, title, e)
            valid_tracks.append(track)

    return valid_tracks, invalid_tracks


async def fetch_lastfm_tag_tracks(tag: str, limit: int = 50) -> list:
    """Fetches the top tracks for a given tag/genre from Last.fm.

    Args:
        tag: The genre/tag name (e.g. 'synthwave', 'lofi').
        limit: Number of tracks to retrieve.

    Returns:
        List of dictionaries with 'artist' and 'title'.
    """
    encoded_tag = urllib.parse.quote(tag)
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=tag.gettoptracks"
        f"&tag={encoded_tag}&api_key={LASTFM_KEY}&format=json&limit={limit}"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            tracks_data = data.get("tracks", {}).get("track", [])
            return [
                {"artist": t.get("artist", {}).get("name", ""), "title": t.get("name", "")}
                for t in tracks_data
                if t.get("artist", {}).get("name") and t.get("name")
            ]
    except Exception as e:
        logger.error("Error fetching Last.fm tag tracks: %s", e)
    return []


async def fetch_lastfm_similar_tracks(artist: str, title: str, limit: int = 50) -> list:
    """Fetches tracks similar to the specified track from Last.fm.

    Args:
        artist: Artist name.
        title: Track title.
        limit: Number of tracks to retrieve.

    Returns:
        List of similar tracks.
    """
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=track.getsimilar"
        f"&artist={urllib.parse.quote(artist)}"
        f"&track={urllib.parse.quote(title)}"
        f"&api_key={LASTFM_KEY}&format=json&limit={limit}"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            tracks_data = data.get("similartracks", {}).get("track", [])
            return [
                {"artist": t.get("artist", {}).get("name", ""), "title": t.get("name", "")}
                for t in tracks_data
                if t.get("artist", {}).get("name") and t.get("name")
            ]
    except Exception as e:
        logger.error("Error fetching Last.fm similar tracks: %s", e)
    return []


async def fetch_lastfm_similar_artists_tracks(artist: str, limit: int = 30) -> list:
    """Fetches similar artists from Last.fm, and aggregates their top tracks.

    Args:
        artist: Artist name.
        limit: Target number of tracks to retrieve in total.

    Returns:
        List of tracks from similar artists.
    """
    import random

    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar"
        f"&artist={urllib.parse.quote(artist)}"
        f"&api_key={LASTFM_KEY}&format=json&limit=10"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code != 200:
            return []
        data = response.json()
        similar_artists = data.get("similarartists", {}).get("artist", [])
        if not similar_artists:
            return []

        tracks = []
        for sa in similar_artists[:5]:
            sa_name = sa.get("name", "")
            if not sa_name:
                continue
            sa_url = (
                f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks"
                f"&artist={urllib.parse.quote(sa_name)}"
                f"&api_key={LASTFM_KEY}&format=json&limit=8"
            )
            try:
                sa_resp = await asyncio.to_thread(requests.get, sa_url, timeout=5)
                if sa_resp.status_code == 200:
                    sa_data = sa_resp.json()
                    sa_tracks = sa_data.get("toptracks", {}).get("track", [])
                    for t in sa_tracks:
                        if t.get("name"):
                            tracks.append({"artist": sa_name, "title": t["name"]})
            except Exception as e:
                logger.warning("Error fetching top tracks for similar artist %s: %s", sa_name, e)

        random.shuffle(tracks)
        return tracks[:limit]
    except Exception as e:
        logger.error("Error fetching Last.fm similar artists: %s", e)
    return []


async def _fetch_musicbrainz_new_releases(genre: str = "", limit: int = 25) -> list:
    """Queries MusicBrainz for recent album/single releases from the last 60 days.

    Args:
        genre: Optional genre/tag filter.
        limit: Max results to return.

    Returns:
        List of dicts with 'artist' and 'title' (album name used as seed).
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    start_date = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    # MusicBrainz Lucene search — filter by release date and optionally by tag
    query = f"date:[{start_date} TO {end_date}]"
    if genre:
        query += f" AND tag:{urllib.parse.quote(genre)}"

    url = (
        f"https://musicbrainz.org/ws/2/release-group"
        f"?query={urllib.parse.quote(query, safe='[](): ')}"
        f"&type=album|single|ep&fmt=json&limit={limit}"
    )

    headers = {
        "User-Agent": "SopheeAgent/1.0 (Discord bot; contact: github.com/sophee-agent)",
        "Accept": "application/json",
    }

    try:
        response = await asyncio.to_thread(
            requests.get, url, headers=headers, timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            release_groups = data.get("release-groups", [])

            results = []
            for rg in release_groups:
                title = rg.get("title", "")
                artist_credit = rg.get("artist-credit", [])
                if artist_credit and title:
                    artist_name = artist_credit[0].get("name", "")
                    if artist_name:
                        results.append({
                            "artist": artist_name,
                            "album": title,
                            "type": rg.get("primary-type", "Album"),
                            "date": rg.get("first-release-date", ""),
                        })
            return results
        else:
            logger.warning("MusicBrainz returned status %d", response.status_code)
    except Exception as e:
        logger.error("Error fetching MusicBrainz releases: %s", e)

    return []


async def _get_tracks_from_releases(releases: list) -> list:
    """Takes MusicBrainz release data and gets playable tracks via Last.fm.

    For each release, fetches the artist's top tracks from Last.fm to get
    actual song titles (MusicBrainz release-groups give album names, not tracks).
    """
    import random

    tracks = []
    seen_artists = set()

    for release in releases:
        artist = release.get("artist", "")
        if not artist or artist.lower() in seen_artists:
            continue
        seen_artists.add(artist.lower())

        # Get top tracks for this artist from Last.fm
        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks"
            f"&artist={urllib.parse.quote(artist)}"
            f"&api_key={LASTFM_KEY}&format=json&limit=5"
        )
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=5)
            if resp.status_code == 200:
                resp_data = resp.json()
                artist_tracks = resp_data.get("toptracks", {}).get("track", [])
                if artist_tracks:
                    # Pick a random track (not always #1) for variety
                    chosen = random.choice(artist_tracks[:3]) if len(artist_tracks) >= 3 else artist_tracks[0]
                    tracks.append({
                        "artist": artist,
                        "title": chosen.get("name", ""),
                    })
        except Exception as e:
            logger.warning("Error fetching Last.fm tracks for %s: %s", artist, e)

        # Respect MusicBrainz rate limit (1 req/sec)
        await asyncio.sleep(0.3)

    return tracks


async def fetch_new_music_releases(genre: str = "") -> list:
    """Fetches recent music releases using MusicBrainz (structured data, no hallucination).
    Falls back to Gemini Search Grounding if MusicBrainz returns nothing.

    Args:
        genre: Optional music genre filter (e.g. 'synthwave', 'rock').

    Returns:
        List of dictionaries with 'artist' and 'title'.
    """
    # Primary: MusicBrainz structured data
    releases = await _fetch_musicbrainz_new_releases(genre=genre, limit=25)
    if releases:
        tracks = await _get_tracks_from_releases(releases)
        if tracks:
            logger.info(
                "Got %d tracks from MusicBrainz new releases (genre=%s)",
                len(tracks), genre or "all",
            )
            return tracks

    # Fallback: Gemini Search Grounding (for genres MusicBrainz tags poorly)
    logger.info("MusicBrainz returned no results, falling back to Gemini Search")
    from pydantic import BaseModel

    class NewReleaseTrack(BaseModel):
        artist: str
        title: str

    class NewReleasesResponse(BaseModel):
        tracks: list[NewReleaseTrack]

    token = os.getenv("GEMINI_TOKEN", os.getenv("GEMINI_API_KEY", ""))
    client = genai.Client(api_key=token)

    query = "What are some notable album or track music new releases from this month or last month?"
    if genre:
        query = f"What are some notable new music releases (albums or tracks) in the {genre} genre from this month or last month?"

    try:
        def generate():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=query,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    response_mime_type="application/json",
                    response_schema=NewReleasesResponse,
                ),
            )

        response = await asyncio.to_thread(generate)
        if response.text:
            data = json.loads(response.text)
            candidates = data.get("tracks", [])
            valid, _ = await validate_tracklist_via_lastfm(candidates)
            return valid
    except Exception as e:
        logger.error("Error in Gemini fallback for new releases: %s", e)

    return []


# ---------------------------------------------------------------------------
# Last.fm Primitive Tools (agent-facing — the LLM can compose these freely)
# ---------------------------------------------------------------------------

async def search_lastfm(query: str, tool_context: ToolContext) -> dict:
    """Searches Last.fm for tracks, artists, or albums matching a query.
    Use this for general music lookups when you need to find specific tracks or artists.

    Args:
        query: The search query (e.g. 'Radiohead', 'Bohemian Rhapsody', 'dream pop').

    Returns:
        A dictionary with matching tracks and artists from Last.fm.
    """
    results = {"tracks": [], "artists": []}

    # Search tracks
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=track.search"
        f"&track={urllib.parse.quote(query)}"
        f"&api_key={LASTFM_KEY}&format=json&limit=10"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            track_matches = data.get("results", {}).get("trackmatches", {}).get("track", [])
            results["tracks"] = [
                {
                    "artist": t.get("artist", ""),
                    "title": t.get("name", ""),
                    "listeners": t.get("listeners", "0"),
                }
                for t in track_matches
                if t.get("artist") and t.get("name")
            ]
    except Exception as e:
        logger.error("Error searching Last.fm tracks: %s", e)

    # Search artists
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=artist.search"
        f"&artist={urllib.parse.quote(query)}"
        f"&api_key={LASTFM_KEY}&format=json&limit=5"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            artist_matches = data.get("results", {}).get("artistmatches", {}).get("artist", [])
            results["artists"] = [
                {
                    "name": a.get("name", ""),
                    "listeners": a.get("listeners", "0"),
                }
                for a in artist_matches
                if a.get("name")
            ]
    except Exception as e:
        logger.error("Error searching Last.fm artists: %s", e)

    return {"status": "success", "results": results}


async def get_artist_info(artist: str, tool_context: ToolContext) -> dict:
    """Gets detailed information about an artist from Last.fm including bio,
    tags, similar artists, and play counts. Use this to learn about an artist
    the LLM may not know well, or to find related artists and genres.

    Args:
        artist: The artist name (e.g. 'Radiohead', 'Billie Eilish').

    Returns:
        A dictionary with the artist's bio, tags, similar artists, and stats.
    """
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=artist.getinfo"
        f"&artist={urllib.parse.quote(artist)}"
        f"&api_key={LASTFM_KEY}&format=json&autocorrect=1"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            artist_data = data.get("artist", {})

            # Extract tags
            tags = [t.get("name", "") for t in artist_data.get("tags", {}).get("tag", [])]

            # Extract similar artists
            similar = [
                s.get("name", "")
                for s in artist_data.get("similar", {}).get("artist", [])
            ]

            # Extract bio summary (strip HTML)
            bio_raw = artist_data.get("bio", {}).get("summary", "")
            import re
            bio = re.sub(r"<[^>]+>", "", bio_raw).strip()
            if len(bio) > 800:
                bio = bio[:800] + "..."

            return {
                "status": "success",
                "artist": artist_data.get("name", artist),
                "listeners": artist_data.get("stats", {}).get("listeners", "0"),
                "playcount": artist_data.get("stats", {}).get("playcount", "0"),
                "tags": tags,
                "similar_artists": similar,
                "bio": bio,
            }
    except Exception as e:
        logger.error("Error fetching artist info: %s", e)

    return {"status": "error", "message": f"Could not find info for '{artist}'."}


async def get_track_info(artist: str, title: str, tool_context: ToolContext) -> dict:
    """Gets detailed information about a specific track from Last.fm including
    tags, play count, album, and wiki summary. Use this to learn about a track's
    genre, context, and reception.

    Args:
        artist: The artist name.
        title: The track title.

    Returns:
        A dictionary with the track's tags, play count, album info, and wiki.
    """
    url = (
        f"http://ws.audioscrobbler.com/2.0/?method=track.getinfo"
        f"&artist={urllib.parse.quote(artist)}"
        f"&track={urllib.parse.quote(title)}"
        f"&api_key={LASTFM_KEY}&format=json&autocorrect=1"
    )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            track_data = data.get("track", {})

            tags = [t.get("name", "") for t in track_data.get("toptags", {}).get("tag", [])]

            wiki_raw = track_data.get("wiki", {}).get("summary", "")
            import re
            wiki = re.sub(r"<[^>]+>", "", wiki_raw).strip()
            if len(wiki) > 600:
                wiki = wiki[:600] + "..."

            album_data = track_data.get("album", {})

            return {
                "status": "success",
                "artist": track_data.get("artist", {}).get("name", artist),
                "title": track_data.get("name", title),
                "listeners": track_data.get("listeners", "0"),
                "playcount": track_data.get("playcount", "0"),
                "tags": tags,
                "album": album_data.get("title", "Unknown"),
                "wiki": wiki if wiki else "No wiki available.",
            }
    except Exception as e:
        logger.error("Error fetching track info: %s", e)

    return {"status": "error", "message": f"Could not find info for '{artist} - {title}'."}


async def get_trending_tracks(country: str = "", tool_context: ToolContext = None) -> dict:
    """Gets the current globally trending tracks from Last.fm weekly charts.
    Optionally filter by country for regional trends.
    Use this when the user asks what's popular right now, what's trending,
    or wants a playlist based on current hits.

    Args:
        country: Optional ISO 3166-1 country name (e.g. 'united states', 'japan', 'germany').
                 Leave empty for global charts.

    Returns:
        A dictionary with the top trending tracks this week.
    """
    if country:
        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=geo.gettoptracks"
            f"&country={urllib.parse.quote(country)}"
            f"&api_key={LASTFM_KEY}&format=json&limit=20"
        )
    else:
        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=chart.gettoptracks"
            f"&api_key={LASTFM_KEY}&format=json&limit=20"
        )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if country:
                tracks_data = data.get("tracks", {}).get("track", [])
            else:
                tracks_data = data.get("tracks", {}).get("track", [])

            tracks = [
                {
                    "artist": t.get("artist", {}).get("name", t.get("artist", "")),
                    "title": t.get("name", ""),
                    "listeners": t.get("listeners", "0"),
                    "playcount": t.get("playcount", "0"),
                }
                for t in tracks_data
                if t.get("name")
            ]

            region = country.title() if country else "Global"
            return {
                "status": "success",
                "region": region,
                "trending_tracks": tracks,
            }
    except Exception as e:
        logger.error("Error fetching trending tracks: %s", e)

    return {"status": "error", "message": "Could not fetch trending tracks."}


async def get_trending_artists(country: str = "", tool_context: ToolContext = None) -> dict:
    """Gets the current globally trending artists from Last.fm weekly charts.
    Optionally filter by country for regional trends.
    Use this when the user asks who's popular right now or wants to discover trending artists.

    Args:
        country: Optional ISO 3166-1 country name (e.g. 'united states', 'japan', 'germany').
                 Leave empty for global charts.

    Returns:
        A dictionary with the top trending artists this week.
    """
    if country:
        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=geo.gettopartists"
            f"&country={urllib.parse.quote(country)}"
            f"&api_key={LASTFM_KEY}&format=json&limit=20"
        )
    else:
        url = (
            f"http://ws.audioscrobbler.com/2.0/?method=chart.gettopartists"
            f"&api_key={LASTFM_KEY}&format=json&limit=20"
        )
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if country:
                artists_data = data.get("topartists", {}).get("artist", [])
            else:
                artists_data = data.get("artists", {}).get("artist", [])

            artists = [
                {
                    "name": a.get("name", ""),
                    "listeners": a.get("listeners", "0"),
                    "playcount": a.get("playcount", "0"),
                }
                for a in artists_data
                if a.get("name")
            ]

            region = country.title() if country else "Global"
            return {
                "status": "success",
                "region": region,
                "trending_artists": artists,
            }
    except Exception as e:
        logger.error("Error fetching trending artists: %s", e)

    return {"status": "error", "message": "Could not fetch trending artists."}


# ---------------------------------------------------------------------------
# Station Management
# ---------------------------------------------------------------------------

async def start_radio_station(playlist_thesis: str, tool_context: ToolContext, mode: str = "standard") -> dict:
    """Curates a new radio station playlist and stages it for launch.
    ONLY call this when NO station is currently running. If a station is already
    active, the model should use `steer_radio` to change direction instead.

    This tool curates validated tracks via LLM + Last.fm, then stages them
    in session state for the Discord embed to render. The user then clicks
    "Automate with DJ" or "Pure Music" to actually start playback.

    Args:
        playlist_thesis: The theme, concept, vibe, or thesis of the music requested
                         (e.g., 'synthwave', 'rainy day cafe').
        mode: The station mode ('standard', 'discovery_genre', 'discovery_favorites').

    Returns:
        A dictionary containing the curated tracklist, or an error if a station is already active.
    """
    from app.radio_state import is_station_active, resolve_guild_id
    from bot.audio import get_user_favorites

    # --- Station-active guard ---
    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    guild_id = resolve_guild_id(channel_id) or channel_id
    user_id = session.user_id if session else "default_user"

    if is_station_active(guild_id):
        return {
            "status": "station_already_active",
            "message": (
                "A radio station is already running! "
                "Use `steer_radio` to change the music direction, "
                "`add_to_queue` to insert a track, or "
                "`show_station_queue` to see what's coming up. "
                "Do NOT call start_radio_station while a station is active."
            ),
        }

    # --- Curate the playlist ---
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    model_id = "gemini-3.1-flash-lite"

    # Seeding based on mode
    if mode == "discovery_favorites":
        favs = get_user_favorites(user_id)
        liked_tracks = favs.get("liked_tracks", [])
        if not liked_tracks:
            return {
                "status": "error",
                "message": "You don't have any persistent favorites yet! Play some tracks and click the Heart (💖) button to add favorites first.",
            }
        favs_str = "\n".join([f"- {t['artist']} - {t['title']}" for t in liked_tracks])
        prompt = f"""You are a music nerd acting as an internet radio DJ.
TASK:
1. The user has requested a discovery radio station based on their favorites list:
{favs_str}
2. Dig into your vast musical knowledge and select exactly 4 songs that are SIMILAR to their favorites, but NOT the exact same tracks. Include deep cuts and obscure tracks!
3. Generate a list of exactly 3-5 relevant, specific Last.fm genre/style tags that describe the sonic vibe of these favorites, along with a weight (a float between 0.1 and 1.0) indicating how strongly it should influence the station's music. The most central tag should have a weight of 1.0.

STRICT OUTPUT FORMAT (JSON ONLY, no markdown formatting):
{{
  "seed_tags": [
    {{"tag": "tag1", "weight": 1.0}},
    {{"tag": "tag2", "weight": 0.7}}
  ],
  "selected_tracks": [
    {{"artist": "Artist1", "title": "Title1"}},
    {{"artist": "Artist2", "title": "Title2"}},
    {{"artist": "Artist3", "title": "Title3"}},
    {{"artist": "Artist4", "title": "Title4"}}
  ]
}}"""
    else:
        prompt = f"""You are a music nerd acting as an internet radio DJ.
TASK:
1. The user has requested a radio station based on the following theme/thesis: '{playlist_thesis}' with mode: '{mode}'.
2. Dig into your vast musical knowledge and select exactly 4 songs that create a cohesive listening experience for this theme. Feel free to include deep cuts and obscure tracks!
3. Generate a list of exactly 3-5 relevant, specific Last.fm genre/style tags that describe the sonic vibe of this theme. For each tag, assign a weight (a float between 0.1 and 1.0) indicating how strongly it should influence the station's music. The most central tag should have a weight of 1.0.

STRICT OUTPUT FORMAT (JSON ONLY, no markdown formatting):
{{
  "seed_tags": [
    {{"tag": "tag1", "weight": 1.0}},
    {{"tag": "tag2", "weight": 0.7}}
  ],
  "selected_tracks": [
    {{"artist": "Artist1", "title": "Title1"}},
    {{"artist": "Artist2", "title": "Title2"}},
    {{"artist": "Artist3", "title": "Title3"}},
    {{"artist": "Artist4", "title": "Title4"}}
  ]
}}"""

    try:
        interaction = await client.aio.interactions.create(model=model_id, input=prompt)
        text = interaction.output_text
        data = _extract_json(text)

        all_valid_tracks = []
        retries = 0
        current_tracks = data.get("selected_tracks", [])

        # Filter out duplicates from the initial model response
        unique_initial = []
        for track in current_tracks:
            if not is_duplicate(track, unique_initial):
                unique_initial.append(track)
        current_tracks = unique_initial

        while retries < 3:
            valid, invalid = await validate_tracklist_via_lastfm(current_tracks)

            for track in valid:
                if not is_duplicate(track, all_valid_tracks):
                    all_valid_tracks.append(track)

            if len(all_valid_tracks) >= 4:
                break

            needed = 4 - len(all_valid_tracks)
            retries += 1
            logger.info(
                "Station curation: %d valid, need %d more (retry %d/3)",
                len(all_valid_tracks), needed, retries,
            )

            already_chosen_str = "\n".join(
                [f"- {t.get('artist')} - {t.get('title')}" for t in all_valid_tracks]
            )
            invalid_list_str = "\n".join(
                [f"- {t.get('artist')} - {t.get('title')}" for t in invalid]
            )

            retry_prompt = f"""We currently have the following {len(all_valid_tracks)} validated unique tracks:
{already_chosen_str}

The following generated tracks were rejected (not found on Last.FM or are not actual songs):
{invalid_list_str}

Please generate exactly {needed} NEW valid, playable tracks.
CRITICAL RULES:
1. DO NOT repeat any of the already chosen tracks listed above. They must be completely new and unique.
2. DO NOT repeat any of the rejected tracks listed above.
3. Ensure there are no duplicate songs in your output.

Use the same JSON array format."""

            interaction = await client.aio.interactions.create(
                model=model_id,
                previous_interaction_id=interaction.id,
                input=retry_prompt,
            )
            text = interaction.output_text
            new_data = _extract_json(text)
            current_tracks = new_data.get("selected_tracks", [])

            # Filter out duplicates from the newly generated tracks
            unique_new = []
            for track in current_tracks:
                if not is_duplicate(track, unique_new) and not is_duplicate(track, all_valid_tracks):
                    unique_new.append(track)
            current_tracks = unique_new

        final_tracks = all_valid_tracks[:4]

        # Stage in session state for the Discord embed to pick up (flat keys)
        tool_context.state["staged_station_tracks"] = final_tracks
        tool_context.state["staged_station_thesis"] = playlist_thesis
        tool_context.state["staged_station_mode"] = mode
        tool_context.state["staged_station_seed_tags"] = data.get("seed_tags", [])

        return {
            "status": "success",
            "playlist_thesis": playlist_thesis,
            "tracks": final_tracks,
            "message": "Station playlist curated and ready. The user will see a launch embed with playback options.",
        }
    except Exception as e:
        logger.error("Error curating station: %s", e)
        return {"status": "error", "message": f"Error curating station: {e}"}


# ---------------------------------------------------------------------------
# TTS Generation
# ---------------------------------------------------------------------------

async def generate_tts(text: str, tool_context: ToolContext) -> dict:
    """Generates a Text-To-Speech (TTS) audio file based on a string of text.
    Saves the output audio file to the session's artifacts as 'tts.wav'.

    Args:
        text: The text to be converted to speech.

    Returns:
        A dictionary containing the artifact name.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-tts-preview",
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Laomedeia"
                        )
                    )
                ),
            ),
        )

        audio_bytes = _extract_audio_bytes(response)

        if audio_bytes:
            wav_data = _pcm_to_wav(audio_bytes)
            part = types.Part(
                inline_data=types.Blob(mime_type="audio/wav", data=wav_data)
            )
            artifact_name = f"tts_{hashlib.md5(text.encode()).hexdigest()[:8]}.wav"
            await tool_context.save_artifact(artifact_name, part)
            tool_context.state["latest_tts_artifact"] = artifact_name

            return {
                "status": "success",
                "artifact_name": artifact_name,
                "message": "Audio successfully generated and saved to artifacts.",
            }
        else:
            return {"status": "error", "message": "Failed to generate audio."}
    except Exception as e:
        return {"status": "error", "message": f"Error generating audio: {e}"}


async def generate_tts_script(context: str, tool_context: ToolContext) -> dict:
    """Generates a detailed script based on context using a specialized prompt, then converts the transcript to speech.
    Saves the script to state and the audio file to the session's artifacts.

    Args:
        context: The context, topic, or instructions for the script.

    Returns:
        A dictionary containing the generated script text and the audio artifact name.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    model_id = "gemini-3.1-flash-lite"

    script_prompt = f"""You are a scriptwriter and audio director. I have a simple context but NO TRANSCRIPT.

TASK:
1. Write a creative, engaging radio script based on the given context.
2. Format the entire output as a structured TTS prompt. Follow the strict output format exactly.

You may include emotion and interjection tags in brackets within the script to direct the TTS model's performance. For example, you can write: "[amused] Oh, really?" or "[sigh] I suppose so".

STRICT OUTPUT FORMAT:

# AUDIO PROFILE: Sophee
## "Radio Host Segment"

## THE SCENE: Internet Radio Station
Sophee is live on air, broadcasting her carefully curated playlist.

### DIRECTOR'S NOTES
Style: Insightful, observant, dry humor, respectful.
Pace: Conversational, relaxed.
Accent: Neutral American.

### SAMPLE CONTEXT
You are Sophee, the curator and host of this internet radio station.
Sophee is an insightful music enthusiast rather than a traditional DJ.
She builds playlists intentionally and enjoys exploring connections between artists, production, regional scenes, and musical history.
Her humor is dry, observant, and occasionally surreal.
She treats all genres with genuine respect.

Write for spoken delivery. Use short paragraphs and varied sentence lengths.
You may include occasional performance tags in brackets, such as [thoughtful], [amused], [soft laugh], or [conspiratorial].

#### TRANSCRIPT
[Script]

----------------

INPUT CONTEXT:
{context}

CRITICAL RULE:
Ensure the divider "#### TRANSCRIPT" is used exactly as written before the spoken text."""

    try:
        script_interaction = await client.aio.interactions.create(
            model=model_id, input=script_prompt
        )
        full_script = script_interaction.output_text

        # Convert the script to audio
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-tts-preview",
            contents=full_script.strip(),
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Laomedeia"
                        )
                    )
                ),
            ),
        )

        audio_bytes = _extract_audio_bytes(response)

        if audio_bytes:
            wav_data = _pcm_to_wav(audio_bytes)
            part = types.Part(
                inline_data=types.Blob(mime_type="audio/wav", data=wav_data)
            )
            artifact_name = f"script_tts_{hashlib.md5(full_script.encode()).hexdigest()[:8]}.wav"
            await tool_context.save_artifact(artifact_name, part)
            tool_context.state["latest_tts_artifact"] = artifact_name
            tool_context.state["last_tts_script"] = full_script

            return {
                "status": "success",
                "script": full_script,
                "artifact_name": artifact_name,
                "message": "Script and audio successfully generated and saved.",
            }
        else:
            return {"status": "error", "message": "Failed to generate audio from script."}

    except Exception as e:
        return {"status": "error", "message": f"Error in generate_tts_script: {e}"}


# ---------------------------------------------------------------------------
# Image Generation
# ---------------------------------------------------------------------------

async def generate_image(prompt: str, tool_context: ToolContext, resolution: str = "0.5k") -> dict:
    """Generates a high-quality image based on a detailed text prompt.
    Saves the output image to the user's artifacts (persistent across sessions).

    Args:
        prompt: The detailed description of the image to generate.
        resolution: The resolution of the image to generate. Supported values:
                    - '0.5k' (default, generates a 512x512 image)
                    - '1k' (generates a 1024x1024 image)

    Returns:
        A dictionary containing the generated image's artifact name.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    try:
        start_fresh = tool_context.state.get("start_fresh_image", False)
        if start_fresh:
            tool_context.state["start_fresh_image"] = False
            prev_id = None
        else:
            prev_id = tool_context.state.get("last_image_interaction_id")

        # Check if there is a cached reference image in session state
        latest_img = tool_context.state.get("latest_input_image")
        if latest_img:
            input_data = [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "data": latest_img["data"],
                    "mime_type": latest_img["mime_type"],
                },
            ]
        else:
            input_data = prompt

        # Map resolution to the model's native image_size string
        api_image_size = "512"
        if resolution.lower().strip() == "1k":
            api_image_size = "1K"

        tool_context.state["latest_resolution"] = resolution

        kwargs = {
            "model": "gemini-3.1-flash-image",
            "input": input_data,
            "response_format": {"type": "image"},
            "generation_config": types.GenerateContentConfig(
                image_config=types.ImageConfig(image_size=api_image_size)
            ),
        }
        if prev_id:
            kwargs["previous_interaction_id"] = prev_id

        image_interaction = await client.aio.interactions.create(**kwargs)  # type: ignore

        # Save the interaction id for multi-turn editing
        tool_context.state["last_image_interaction_id"] = image_interaction.id

        image_bytes = None
        for img_step in image_interaction.steps:
            if img_step.type == "model_output":
                for content_block in img_step.content:
                    if content_block.type == "image":
                        image_bytes = base64.b64decode(content_block.data)
                        break

        if image_bytes:
            part = types.Part(
                inline_data=types.Blob(mime_type="image/jpeg", data=image_bytes)
            )
            artifact_name = f"user:generated_image_{hashlib.md5(prompt.encode()).hexdigest()[:8]}.jpeg"
            await tool_context.save_artifact(artifact_name, part)

            return {
                "status": "success",
                "artifact_name": artifact_name,
                "message": "Image successfully generated and saved.",
            }
        else:
            return {"status": "error", "message": "No image generated."}
    except Exception as e:
        return {"status": "error", "message": f"Error generating image: {e}"}


# ---------------------------------------------------------------------------
# Art Director Helpers
# ---------------------------------------------------------------------------

async def get_now_playing(tool_context: ToolContext) -> dict:
    """Retrieves the details of the song currently playing in the voice channel.

    Returns:
        A dictionary containing the active song title and artist.
    """
    from app.radio_state import now_playing_cache, resolve_guild_id

    session = tool_context.session
    session_id = session.id if session else ""
    channel_id_str = session_id.replace("discord_", "")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = 9999
    guild_id = resolve_guild_id(channel_id) or channel_id

    current_song = now_playing_cache.get(guild_id, None)
    if current_song:
        return {"status": "success", "now_playing": current_song}
    return {"status": "info", "now_playing": "Nothing is currently playing."}


async def roll_artistic_inspiration(tool_context: ToolContext) -> dict:
    """Selects one random artist from each of the three visual dimensions (medium, lighting, genre)
    from the artists catalog, saves them to session state, and returns them.

    Returns:
        A dictionary containing the rolled artists for each category.
    """
    import random

    catalog_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artists_catalog.json"
    )
    if not os.path.exists(catalog_path):
        return {"status": "error", "message": "Artists catalog not found."}

    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)

        mediums = [name for name, cat in catalog.items() if cat == "medium_and_line"]
        lightings = [name for name, cat in catalog.items() if cat == "lighting_and_atmosphere"]
        genres = [name for name, cat in catalog.items() if cat == "genre_and_subject"]

        if not mediums or not lightings or not genres:
            return {"status": "error", "message": "Catalog categories are empty."}

        rolled = {
            "medium": random.choice(mediums),
            "lighting": random.choice(lightings),
            "genre": random.choice(genres),
        }

        tool_context.state["rolled_style"] = rolled
        return {"status": "success", "rolled_style": rolled}
    except Exception as e:
        return {"status": "error", "message": f"Error rolling artistic inspiration: {e}"}


async def get_art_director_settings(tool_context: ToolContext) -> dict:
    """Retrieves the current settings for the art director, including style roll,
    creative mode flags, previously rolled styles, and the latest resolution.

    Returns:
        A dictionary containing the settings.
    """
    return {
        "status": "success",
        "force_style_roll": tool_context.state.get("force_style_roll", False),
        "art_director_mode": tool_context.state.get("art_director_mode", "simple"),
        "rolled_style": tool_context.state.get("rolled_style", None),
        "latest_resolution": tool_context.state.get("latest_resolution", "0.5k"),
    }
