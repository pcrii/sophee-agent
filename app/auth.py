import os
import json
import logging
from typing import Optional
from ytmusicapi import YTMusic
from ytmusicapi.auth.oauth import OAuthCredentials, RefreshingToken
import asyncio
import discord

logger = logging.getLogger(__name__)

AUTH_DIR = "data/auth"
os.makedirs(AUTH_DIR, exist_ok=True)

def get_ytm_client(user_id: str | int) -> Optional[YTMusic]:
    """Returns an authenticated YTMusic client for a specific user, or None if not linked."""
    filepath = os.path.join(AUTH_DIR, f"{user_id}_oauth.json")
    if os.path.exists(filepath):
        try:
            client_id = os.environ.get("YOUTUBE_CLIENT_ID")
            client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
            if client_id and client_secret:
                import requests
                creds = OAuthCredentials(client_id, client_secret, requests.Session())
                return YTMusic(filepath, oauth_credentials=creds)
            else:
                return YTMusic(filepath)
        except Exception as e:
            logger.error(f"Failed to load YTMusic for user {user_id}: {e}")
    return None

async def start_oauth_flow(user_id: str | int, interaction: discord.Interaction):
    """Starts the device code OAuth flow via Discord."""
    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        await interaction.response.send_message("❌ The bot host has not configured Google Cloud OAuth credentials (`YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` in `.env`). Please contact the bot owner.", ephemeral=True)
        return
        
    await interaction.response.send_message("⏳ Generating YouTube Music login link...", ephemeral=True)
    
    try:
        import requests
        session = requests.Session()
        creds = OAuthCredentials(client_id, client_secret, session)
        code = await asyncio.to_thread(creds.get_code)
        
        url = f"{code['verification_url']}?user_code={code['user_code']}"
        
        await interaction.edit_original_response(content=f"🔗 **Link your YouTube Music Account**\n\n1. Go to this link: {url}\n2. Enter the code: **`{code['user_code']}`**\n3. Follow the Google prompts to authorize the bot.\n\n*Waiting for you to complete authorization...* (This will time out in a few minutes)")
        
        # Block and poll in a thread
        import time
        interval = code.get("interval", 5)
        expires_in = code.get("expires_in", 1800)
        start_time = time.time()
        
        raw_token = None
        while time.time() - start_time < expires_in:
            await asyncio.sleep(interval)
            try:
                res = await asyncio.to_thread(creds.token_from_code, code["device_code"])
                if "error" not in res:
                    raw_token = res
                    break
                elif res.get("error") != "authorization_pending":
                    # Some other error like expired token
                    logger.debug(f"OAuth poll error: {res}")
            except Exception as e:
                logger.debug(f"OAuth poll exception: {e}")
                
        if not raw_token:
            await interaction.followup.send("❌ **Login timed out or was cancelled.** Please try `!ytlogin` again.", ephemeral=True)
            return
            
        # Save it
        filepath = os.path.join(AUTH_DIR, f"{user_id}_oauth.json")
        with open(filepath, "w") as f:
            json.dump(raw_token, f)
            
        await interaction.edit_original_response(content="✅ **Account Linked Successfully!** Your YouTube Music history and likes will now sync.")
        
    except Exception as e:
        logger.error(f"OAuth flow failed: {e}")
        try:
            await interaction.edit_original_response(content=f"❌ **Login failed or timed out.** \nError: {e}")
        except:
            pass
