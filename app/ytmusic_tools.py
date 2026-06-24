import re
import logging
import asyncio
from typing import Optional, Dict, Any, List
from google.adk.tools import ToolContext

# Because ytmusicapi emulates browser requests, it uses requests synchronously.
# We will run these in asyncio.to_thread to prevent blocking the ADK event loop.
from ytmusicapi import YTMusic

logger = logging.getLogger(__name__)

# Initialize a global client (no auth needed for public searches)
yt = YTMusic()

def _extract_video_id(url: str) -> Optional[str]:
    """Extracts a YouTube videoId from standard youtube URLs."""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

async def search_ytmusic_track(query: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Searches YouTube Music for a track using fuzzy logic. Use this as a validator
    if a user requests a song and you need to find its exact canonical spelling, artist list, 
    and unique videoId.

    Args:
        query: The song and artist to search for (e.g. "Starboy ft daft punk")

    Returns:
        A dictionary containing the top track match's metadata.
    """
    logger.info(f"YTMusic search track: {query}")
    try:
        # Check if the query is a direct YouTube URL
        video_id = _extract_video_id(query)
        if video_id:
            logger.info(f"Extracted videoId from URL: {video_id}")
            song = await asyncio.to_thread(yt.get_song, video_id)
            details = song.get("videoDetails", {})
            if not details:
                return {"status": "error", "message": f"Could not fetch details for videoId {video_id}"}
                
            return {
                "status": "success",
                "title": details.get("title"),
                "artists": [details.get("author")],
                "album": None, # get_song doesn't reliably return album
                "videoId": details.get("videoId"),
                "duration": details.get("lengthSeconds"),
                "isExplicit": False
            }

        # Otherwise do a fuzzy text search
        results = await asyncio.to_thread(yt.search, query, filter="songs")
        if not results:
            return {"status": "error", "message": f"No tracks found for '{query}'"}
            
        top_match = results[0]
        artists = [a.get("name") for a in top_match.get("artists", [])]
        
        return {
            "status": "success",
            "title": top_match.get("title"),
            "artists": artists,
            "album": top_match.get("album", {}).get("name"),
            "videoId": top_match.get("videoId"),
            "duration": top_match.get("duration"),
            "isExplicit": top_match.get("isExplicit", False)
        }
    except Exception as e:
        logger.error(f"YTMusic track search error: {e}")
        return {"status": "error", "message": str(e)}

async def search_ytmusic_artist(query: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Searches YouTube Music for an artist. Returns their browseId (channelId) and name.
    
    Args:
        query: The artist name to search for.
        
    Returns:
        A dictionary containing the top artist match.
    """
    logger.info(f"YTMusic search artist: {query}")
    try:
        results = await asyncio.to_thread(yt.search, query, filter="artists")
        if not results:
            return {"status": "error", "message": f"No artists found for '{query}'"}
            
        top_match = results[0]
        return {
            "status": "success",
            "artist": top_match.get("artist"),
            "browseId": top_match.get("browseId")
        }
    except Exception as e:
        logger.error(f"YTMusic artist search error: {e}")
        return {"status": "error", "message": str(e)}

async def generate_ytmusic_radio(video_id: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Uses YouTube Music's powerful recommendation algorithm to generate a playlist 
    of ~25 similar songs based on a seed track's videoId. 
    Use this to build out intelligent queues or respond to "play songs like X".

    Args:
        video_id: The unique videoId of the seed track (obtainable via search_ytmusic_track).

    Returns:
        A dictionary containing the generated radio tracks and the playlist_url. You MUST include the playlist_url in your response to the user so it embeds the playlist!
    """
    logger.info(f"YTMusic generate radio for videoId: {video_id}")
    try:
        radio = await asyncio.to_thread(yt.get_watch_playlist, video_id)
        if not radio or "tracks" not in radio:
            return {"status": "error", "message": "Failed to generate radio playlist."}
            
        tracks = []
        for t in radio["tracks"]:
            artists = [a.get("name") for a in t.get("artists", [])]
            tracks.append({
                "title": t.get("title"),
                "artists": artists,
                "videoId": t.get("videoId"),
                "duration": t.get("length")
            })
            
        return {
            "status": "success",
            "seed_videoId": video_id,
            "playlist_url": f"https://music.youtube.com/watch?v={video_id}&list=RDAMVM{video_id}",
            "tracks": tracks
        }
    except Exception as e:
        logger.error(f"YTMusic generate radio error: {e}")
        return {"status": "error", "message": str(e)}

async def get_ytmusic_similar_artists(browse_id: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Fetches YouTube Music's 'Fans might also like' list for an artist using their browseId.
    
    Args:
        browse_id: The unique browseId of the artist (obtainable via search_ytmusic_artist).

    Returns:
        A list of similar artists.
    """
    logger.info(f"YTMusic get similar artists for browseId: {browse_id}")
    try:
        artist_data = await asyncio.to_thread(yt.get_artist, browse_id)
        if not artist_data or "related" not in artist_data:
            return {"status": "error", "message": "No related artists found."}
            
        related = artist_data["related"].get("results", [])
        similar_artists = [a.get("title") for a in related]
        
        return {
            "status": "success",
            "browseId": browse_id,
            "similar_artists": similar_artists
        }
    except Exception as e:
        logger.error(f"YTMusic similar artists error: {e}")
        return {"status": "error", "message": str(e)}

async def get_ytmusic_charts(country: str = "US", tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Fetches the top trending charts from YouTube Music.
    
    Args:
        country: The 2-letter ISO country code (e.g. 'US', 'ZZ' for global). Defaults to 'US'.
        
    Returns:
        A dictionary containing top trending tracks and artists.
    """
    logger.info(f"YTMusic get charts for country: {country}")
    try:
        charts = await asyncio.to_thread(yt.get_charts, country)
        
        # Extract tracks
        track_items = charts.get("videos", {}).get("items", [])[:20]
        tracks = []
        for t in track_items:
            artists = [a.get("name") for a in t.get("artists", [])]
            tracks.append({
                "title": t.get("title"),
                "artists": artists,
                "videoId": t.get("videoId")
            })
            
        return {
            "status": "success",
            "country": country,
            "trending_tracks": tracks
        }
    except Exception as e:
        logger.error(f"YTMusic charts error: {e}")
        return {"status": "error", "message": str(e)}

async def get_ytmusic_mood_playlists(category: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Fetches official YouTube Music curated playlists for a specific mood or genre category.
    To see available categories, you must first call this with category="" (empty string).
    
    Args:
        category: The name of the mood or genre category (e.g. 'Hip Hop', 'Chill', 'New Releases').
                  If left empty, returns a list of all valid categories.
                  
    Returns:
        A dictionary containing official playlist names and descriptions, or a list of categories.
    """
    logger.info(f"YTMusic get mood playlists for category: {category}")
    try:
        categories_dict = await asyncio.to_thread(yt.get_mood_categories)
        all_categories = {}
        for group, cat_list in categories_dict.items():
            for c in cat_list:
                all_categories[c["title"].lower()] = c["params"]
                
        if not category:
            return {
                "status": "info",
                "message": "Please provide a valid category. Here are the available options:",
                "available_categories": list(all_categories.keys())
            }
            
        cat_lower = category.lower().strip()
        if cat_lower not in all_categories:
            return {
                "status": "error",
                "message": f"Category '{category}' not found.",
                "available_categories": list(all_categories.keys())
            }
            
        params = all_categories[cat_lower]
        playlists_res = await asyncio.to_thread(yt.get_mood_playlists, params)
        
        playlists = []
        for pl in playlists_res[:10]:
            playlists.append({
                "title": pl.get("title"),
                "description": pl.get("description", ""),
                "subscribers": pl.get("subscribers", ""),
                "playlistId": pl.get("playlistId")
            })
            
        return {
            "status": "success",
            "category": category,
            "playlists": playlists
        }
    except Exception as e:
        logger.error(f"YTMusic mood playlists error: {e}")
        return {"status": "error", "message": str(e)}

async def load_ytmusic_playlist(playlist_id: str, tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """Loads a YouTube Music playlist. If the station's JIT generation is disabled,
    it adds the tracks directly to the upcoming queue (maintaining order for single-artist albums, shuffling otherwise).
    If JIT is enabled, it dumps the tracks into the candidate pool to act as mathematical seeds.
    
    Args:
        playlist_id: The ID of the playlist (starts with PL or RD).
        
    Returns:
        A dictionary containing the loaded status.
    """
    logger.info(f"YTMusic load playlist: {playlist_id}")
    try:
        playlist_data = await asyncio.to_thread(yt.get_playlist, playlist_id, limit=50)
        tracks = playlist_data.get("tracks", [])
        if not tracks:
            return {"status": "error", "message": "No tracks found in playlist."}
            
        parsed_tracks = []
        for t in tracks:
            artists = [a.get("name") for a in t.get("artists", [])]
            parsed_tracks.append({
                "title": t.get("title"),
                "artist": artists[0] if artists else "Unknown Artist",
                "videoId": t.get("videoId")
            })
            
        from app.radio_tools import _get_radio_state
        state = _get_radio_state(tool_context)
        if not state or not state.get("active"):
            return {"status": "error", "message": "No active radio broadcast found."}
            
        jit_enabled = state.get("jit_enabled", True)
        
        if not jit_enabled:
            # Check if single artist (album)
            artists_set = set(t["artist"] for t in parsed_tracks)
            is_single_artist = len(artists_set) == 1
            
            if not is_single_artist:
                import random
                random.shuffle(parsed_tracks)
                
            state.setdefault("upcoming_tracks", []).extend(parsed_tracks)
            return {
                "status": "success",
                "message": f"JIT is OFF. Added {len(parsed_tracks)} tracks directly to the queue. Order maintained: {is_single_artist}."
            }
        else:
            # JIT is ON. Add to candidate pool with high score
            pool = state.setdefault("candidate_pool", [])
            for pt in parsed_tracks:
                # Add to candidate pool with high score so it gets picked
                pool.append((pt, 50))
                
            return {
                "status": "success",
                "message": f"JIT is ON. Seeded {len(parsed_tracks)} playlist tracks into the candidate pool to organically steer the station."
            }
            
    except Exception as e:
        logger.error(f"YTMusic load playlist error: {e}")
        return {"status": "error", "message": str(e)}
