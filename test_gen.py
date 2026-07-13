import asyncio
import os
from google.adk.tools import ToolContext
from app.tools import generate_image
import base64

async def test_gen():
    class MockSession:
        def __init__(self):
            self.id = "test_session"
            self.state = {}
            self.user_id = "test_user"

    class MockToolContext:
        def __init__(self):
            self.state = {
                "latest_input_image": {
                    "data": base64.b64encode(b"dummy image data").decode("utf-8"),
                    "mime_type": "image/jpeg"
                }
            }
            self.session = MockSession()

        async def save_artifact(self, name, part):
            print(f"Artifact saved: {name}")

    ctx = MockToolContext()
    print("Testing generate_image with edit_mode...")
    result = await generate_image(prompt="add a cat", tool_context=ctx)
    print("Result:", result)

if __name__ == "__main__":
    asyncio.run(test_gen())
