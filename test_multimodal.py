import json
import base64
import asyncio
from google.genai import types

async def main():
    try:
        # Create a response that is a list of multimodal dicts
        image_result = [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": base64.b64encode(b"fake_image_data").decode(), "mime_type": "image/png"}
        ]
        
        # Wrap it in a dictionary
        wrapped_response = {"result": image_result}
        
        # Try to make a FunctionResponse out of it
        fr = types.FunctionResponse(name="my_tool", response=wrapped_response)
        part = types.Part.from_function_response(name="my_tool", response=wrapped_response)
        
        print("FunctionResponse dumped:")
        print(fr.model_dump())
        print("Part dumped:")
        print(part.model_dump())
    except Exception as e:
        print("Error creating FunctionResponse:", e)

if __name__ == "__main__":
    asyncio.run(main())
