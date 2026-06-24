import ytmusicapi

print("Starting YouTube Music OAuth setup...")
print("Please follow the instructions below to authenticate the bot.")
ytmusicapi.setup_oauth("oauth.json")
print("Authentication successful! 'oauth.json' has been saved.")
