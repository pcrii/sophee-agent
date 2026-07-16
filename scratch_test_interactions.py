import asyncio
import os
from dotenv import load_dotenv

# Load env variables so we can access API keys
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
load_dotenv()

from app.agent import dj_agent
from google.adk.runners import Runner

from app.db import session_service
from google.genai import types

async def main():
    # Force use of interactions API to see the error
    dj_agent.model.use_interactions_api = True
    
    runner = Runner(agent=dj_agent, app_name="test_app", session_service=session_service)
    print("Running agent...")
    
    # Send a prompt that requires a tool call
    content = types.Content(role="user", parts=[types.Part.from_text(text="what song is playing right now?")])
    async for event in runner.run_async(new_message=content, user_id="test_user", session_id="test_session"):
        print(f"Event: {event}")
    

if __name__ == "__main__":
    asyncio.run(main())
