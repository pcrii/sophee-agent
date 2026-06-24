import os
import logging
from typing import Optional
from ytmusicapi import YTMusic

logger = logging.getLogger(__name__)

def get_ytm_client(user_id: str | int) -> Optional[YTMusic]:
    """Returns an authenticated YTMusic client if the user is the owner, or None if not linked."""
    owner_id = os.environ.get("YTMUSIC_OWNER_ID")
    
    if owner_id and str(user_id) == str(owner_id):
        # The user is the owner, try to load the oauth.json or headers_auth.json
        if os.path.exists("oauth.json"):
            try:
                client_id = os.environ.get("YOUTUBE_CLIENT_ID")
                client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
                if client_id and client_secret:
                    import requests
                    from ytmusicapi.auth.oauth import OAuthCredentials
                    creds = OAuthCredentials(client_id, client_secret, requests.Session())
                    return YTMusic("oauth.json", oauth_credentials=creds)
                else:
                    logger.warning("oauth.json found, but YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET are missing from environment.")
            except Exception as e:
                logger.error(f"Failed to load YTMusic oauth.json for owner: {e}")
                
        if os.path.exists("headers_auth.json"):
            try:
                return YTMusic("headers_auth.json")
            except Exception as e:
                logger.error(f"Failed to load YTMusic headers_auth.json for owner: {e}")
                
        logger.warning(f"Owner requested YTMusic but both oauth.json and headers_auth.json are missing.")
            
    return None
