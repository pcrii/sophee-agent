"""FastAPI server for the Sophee agent API."""

import logging
import os
from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger("sophee.app.fastapi")


class ChatRequest(BaseModel):
    user_id: str
    session_id: str
    message: str


class SuggestionsUpdateRequest(BaseModel):
    contents: str


def create_app():
    """Creates and configures the FastAPI application."""
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.sessions import DatabaseSessionService
    from google.adk.runners import Runner
    from app.agent import root_agent

    app = FastAPI(title="Sophee Agent API")

    session_service = DatabaseSessionService(db_url="sqlite+aiosqlite:///sessions.db")
    artifact_service = InMemoryArtifactService()

    runner = Runner(
        agent=root_agent,
        app_name="app",
        session_service=session_service,
        artifact_service=artifact_service,
    )

    @app.post("/api/chat")
    async def chat(request: ChatRequest):
        """Send a message to the agent and get the text response."""
        from google.genai import types
        response_text = ""
        try:
            new_msg = types.Content(role="user", parts=[types.Part.from_text(text=request.message)])
            async for event in runner.run_async(
                user_id=request.user_id,
                session_id=request.session_id,
                new_message=new_msg,
            ):
                if event.is_final_response():
                    response_parts = (
                        event.content.parts
                        if (event.content and event.content.parts)
                        else []
                    )
                    response_text += "".join([p.text for p in response_parts if p.text])
            return {"status": "success", "response": response_text}
        except Exception as e:
            logger.exception("Error during API chat invocation:")
            return {"status": "error", "message": str(e)}

    @app.get("/api/suggestions")
    async def get_suggestions():
        """Returns the raw contents of the suggestion_box.md file."""
        suggestion_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "suggestion_box.md"
        )
        if not os.path.exists(suggestion_file):
            return {"status": "info", "message": "No suggestions found.", "contents": ""}
        try:
            with open(suggestion_file, encoding="utf-8") as f:
                contents = f.read()
            return {"status": "success", "contents": contents}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.post("/api/suggestions/update")
    async def update_suggestions(request: SuggestionsUpdateRequest):
        """Overwrites the contents of the suggestion_box.md file."""
        suggestion_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "suggestion_box.md"
        )
        os.makedirs(os.path.dirname(suggestion_file), exist_ok=True)
        try:
            with open(suggestion_file, "w", encoding="utf-8") as f:
                f.write(request.contents)
            return {"status": "success", "message": "Suggestions file updated successfully."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/favorites")
    async def get_favorites():
        """Returns the structured favorites data from user_favorites.json."""
        favorites_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "user_favorites.json"
        )
        if not os.path.exists(favorites_file):
            return {"status": "info", "message": "No favorites found.", "favorites": {}}
        try:
            with open(favorites_file, encoding="utf-8") as f:
                import json
                favorites = json.load(f)
            return {"status": "success", "favorites": favorites}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
