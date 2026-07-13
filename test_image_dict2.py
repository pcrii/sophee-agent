import asyncio
import os
from google.genai import Client

async def test():
    client = Client()
    res = await client.aio.interactions.create(
        model='gemini-3.1-flash-lite-image', 
        input='A cute red panda wearing a tiny hat', 
        tools=[{'type': 'google_search', 'search_types': ['web_search']}]
    )
    print('Image:', getattr(res, 'output_image', None))

asyncio.run(test())
