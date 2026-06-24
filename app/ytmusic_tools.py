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
