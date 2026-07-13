import asyncio
import os
import sys
from google.genai import Client
from google.genai.types import Tool, GoogleSearch

async def test_image():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = Client(api_key=api_key)
    try:
        interaction = await client.aio.interactions.create(
            model="gemini-3.1-flash-lite-image",
            input="A cute red panda wearing a tiny hat",
            response_format={"type": "image", "image_size": "1K"},
            tools=[Tool(google_search=GoogleSearch())]
        )
        print("Prefix of data:", interaction.output_image.data[:50] if interaction.output_image else "NO IMAGE")
    except Exception as e:
        print("Error:", type(e), e)

if __name__ == "__main__":
    asyncio.run(test_image())
