"""Utility script to clear all stale Discord slash commands.

Usage:
    1. Make sure your .env has the DISCORD_TOKEN you want to clear commands for
    2. Run: python scripts/clear_slash_commands.py
    3. All global and per-guild slash commands will be removed
    4. You only need to run this once per bot token
"""

import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv()


async def clear_commands():
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user} (ID: {client.user.id})")

        # Clear global commands
        client.tree.clear_commands(guild=None)
        await client.tree.sync()
        print("✅ Global commands cleared")

        # Clear per-guild commands
        for guild in client.guilds:
            client.tree.clear_commands(guild=guild)
            await client.tree.sync(guild=guild)
            print(f"✅ Cleared commands for: {guild.name}")

        print("\nDone! All slash commands have been removed.")
        await client.close()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ DISCORD_TOKEN not found in .env")
        sys.exit(1)

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(clear_commands())
