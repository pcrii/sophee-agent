import logging
import urllib.parse
import asyncio
import time
import requests
from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

MUSICBRAINZ_BASE_URL = "http://musicbrainz.org/ws/2"
USER_AGENT = "SopheeBot/1.0 ( sophee-agent-bot )"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json"
}

# Throttle mechanism to adhere to MusicBrainz's strict 1 req/sec rate limit
_last_request_time = 0.0
_lock = asyncio.Lock()

async def _throttle():
    global _last_request_time
    async with _lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        _last_request_time = time.time()

async def _make_request(url: str) -> dict:
    await _throttle()
    logger.debug(f"MusicBrainz API Call: {url}")
    try:
        response = await asyncio.to_thread(
            requests.get, url, headers=HEADERS, timeout=10
        )
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"MusicBrainz error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"MusicBrainz connection error: {e}")
        return None

async def search_musicbrainz_artist(query: str, tool_context: ToolContext = None) -> dict:
    """Searches MusicBrainz for an artist to find their official MBID (MusicBrainz ID), origin, and lifespan.
    Use this to look up a band or artist's exact ID before querying their discography or relationships.

    Args:
        query: The name of the artist or band to search for.

    Returns:
        A dictionary containing the top artist matches and their MBIDs.
    """
    url = f"{MUSICBRAINZ_BASE_URL}/artist/?query={urllib.parse.quote(query)}&fmt=json"
    data = await _make_request(url)
    if not data or "artists" not in data or len(data["artists"]) == 0:
        return {"status": "error", "message": f"Could not find artist '{query}' in MusicBrainz."}

    results = []
    for artist in data["artists"][:3]:  # Return top 3 matches
        results.append({
            "name": artist.get("name"),
            "mbid": artist.get("id"),
            "type": artist.get("type", "Unknown"),
            "country": artist.get("country", "Unknown"),
            "lifespan": artist.get("life-span", {}),
            "disambiguation": artist.get("disambiguation", "")
        })

    return {"status": "success", "matches": results}

async def get_musicbrainz_artist_releases(mbid: str, tool_context: ToolContext = None) -> dict:
    """Fetches the official Studio Albums and EPs for an artist using their MusicBrainz ID (MBID).
    Unlike Last.fm, this strictly filters out bootlegs, compilations, live albums, and singles, 
    giving you a perfectly accurate official discography.

    Args:
        mbid: The MusicBrainz ID of the artist (obtain this via search_musicbrainz_artist).

    Returns:
        A dictionary containing the official releases.
    """
    url = f"{MUSICBRAINZ_BASE_URL}/release-group/?artist={mbid}&type=album|ep&fmt=json"
    data = await _make_request(url)
    if not data or "release-groups" not in data:
        return {"status": "error", "message": f"Could not find releases for MBID '{mbid}'."}

    releases = []
    for rg in data["release-groups"]:
        # Filter strictly for primary types Album and EP, ignoring secondary types like Compilation, Live, Remix
        primary_type = rg.get("primary-type")
        secondary_types = rg.get("secondary-types", [])
        
        if primary_type in ["Album", "EP"] and len(secondary_types) == 0:
            releases.append({
                "title": rg.get("title"),
                "release_date": rg.get("first-release-date", "Unknown"),
                "type": primary_type,
                "mbid": rg.get("id")
            })
            
    # Sort by release date
    releases.sort(key=lambda x: x.get("release_date", "9999"))

    return {"status": "success", "artist_mbid": mbid, "official_releases": releases}

async def get_musicbrainz_artist_relationships(mbid: str, tool_context: ToolContext = None) -> dict:
    """Fetches relational data for an artist using their MusicBrainz ID (MBID), such as band members, 
    associated acts, official websites, and social media links.

    Args:
        mbid: The MusicBrainz ID of the artist.

    Returns:
        A dictionary containing the artist's relationships and links.
    """
    url = f"{MUSICBRAINZ_BASE_URL}/artist/{mbid}?inc=url-rels+artist-rels&fmt=json"
    data = await _make_request(url)
    if not data:
        return {"status": "error", "message": f"Could not fetch relationships for MBID '{mbid}'."}

    relations = {
        "band_members": [],
        "associated_acts": [],
        "urls": []
    }

    for rel in data.get("relations", []):
        rel_type = rel.get("type")
        if rel.get("target-type") == "artist":
            artist_info = {
                "name": rel.get("artist", {}).get("name"),
                "mbid": rel.get("artist", {}).get("id"),
                "attribute": ", ".join(rel.get("attributes", [])),
                "direction": rel.get("direction")
            }
            if "member of band" in rel_type:
                relations["band_members"].append(artist_info)
            else:
                relations["associated_acts"].append({"type": rel_type, **artist_info})
        
        elif rel.get("target-type") == "url":
            relations["urls"].append({
                "type": rel_type,
                "url": rel.get("url", {}).get("resource")
            })

    return {
        "status": "success",
        "artist": data.get("name"),
        "relations": relations
    }
