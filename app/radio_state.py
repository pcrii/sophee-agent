"""Shared radio state registry.

Both the bot layer (bot/audio.py, bot/views.py) and the ADK agent layer
(app/radio_tools.py) import this module to access live radio state.
This replaces the fragile sys.modules introspection pattern.
"""

import logging

logger = logging.getLogger("sophee.app.radio_state")

# Guild ID -> radio state dict
# State dict shape:
# {
#     "active": bool,
#     "playlist_thesis": str,
#     "genre": str,
#     "upcoming_tracks": [{"artist": str, "title": str}, ...],
#     "played_tracks": [{"artist": str, "title": str}, ...],
#     "current_track": str | None,
# }
active_radios: dict[int, dict] = {}

# Now-playing cache: guild_id -> "Artist - Title"
now_playing_cache: dict[int, str] = {}

# Discord client reference — set by bot/client.py at startup
_discord_client = None


def set_discord_client(client):
    """Called by bot/client.py to register the Discord client for guild lookups."""
    global _discord_client
    _discord_client = client
    logger.info("Discord client registered with radio state")


def get_discord_client():
    """Returns the registered Discord client, or None."""
    return _discord_client


def get_radio_state(guild_id: int) -> dict | None:
    """Returns the radio state for a guild, or None if not active."""
    return active_radios.get(guild_id)


def set_radio_state(guild_id: int, state: dict):
    """Sets/overwrites the radio state for a guild."""
    active_radios[guild_id] = state
    logger.info("Radio state set for guild %s", guild_id)


# Cache of channel ID -> guild ID
channel_guild_cache: dict[int, int] = {}


def register_channel_guild(channel_id: int, guild_id: int):
    """Registers the mapping of a channel ID to a guild ID."""
    channel_guild_cache[channel_id] = guild_id


def resolve_guild_id(channel_id: int) -> int | None:
    """Resolves a Discord channel ID to its guild ID using the registered client."""
    if channel_id in channel_guild_cache:
        return channel_guild_cache[channel_id]

    client = _discord_client
    if not client:
        # Fallback: if only one guild is active, use it
        if len(active_radios) == 1:
            return next(iter(active_radios.keys()))
        return None

    channel = client.get_channel(channel_id)
    if channel and hasattr(channel, "guild") and channel.guild:
        channel_guild_cache[channel_id] = channel.guild.id
        return channel.guild.id

    # Fallback: search all guilds and threads
    for g in client.guilds:
        if g.get_channel(channel_id) is not None or any(
            t.id == channel_id for t in g.threads
        ):
            channel_guild_cache[channel_id] = g.id
            return g.id

    # Last resort: if only one active radio, assume it
    if len(active_radios) == 1:
        return next(iter(active_radios.keys()))

    return None


def is_station_active(guild_id: int) -> bool:
    """Returns True if a radio station is currently broadcasting for this guild."""
    state = active_radios.get(guild_id)
    return bool(state and state.get("active"))
