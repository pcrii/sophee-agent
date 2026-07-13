import asyncio
import os
import sys
from google.genai import Client

async def test_image():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = Client(api_key=api_key)
    try:
        interaction = await client.aio.interactions.create(
            model="gemini-3.1-flash-lite-image",
            input="A cute red panda wearing a tiny hat",
            response_format={"type": "image", "image_size": "1K", "aspect_ratio": "1:1"}
        )
        print("Prefix of data:", interaction.output_image.data[:50])
    except Exception as e:
        print("Error:", type(e), e)

if __name__ == "__main__":
    asyncio.run(test_image())
