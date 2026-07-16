import os, asyncio
from google import genai

async def f():
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    i = await client.aio.interactions.create(model='gemini-3.1-flash-lite', input='Return json [1,2,3]')
    print("TEXT_PROP:", getattr(i, 'text', None))
    print("OUTPUT_TEXT:", getattr(i, 'output_text', None))
    
    print("\n--- STEPS ---")
    for step in i.steps:
        print(type(step))
        if hasattr(step, "model_turn") and step.model_turn:
            print("  parts:", step.model_turn.parts)

asyncio.run(f())
