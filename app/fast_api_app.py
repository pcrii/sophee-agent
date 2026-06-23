"""FastAPI server for the ADK web interface."""

import logging
import os

logger = logging.getLogger("sophee.app.fastapi")


def create_app():
    """Creates and configures the FastAPI application."""
    from google.adk.cli import get_fast_api_app
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.sessions import DatabaseSessionService

    from app.agent import root_agent
    from app.app_utils.typing import Feedback

    # Configure GCP logging if available
    try:
        import google.cloud.logging
        client = google.cloud.logging.Client()
        client.setup_logging()
        logger.info("GCP logging configured")
    except Exception:
        logging.basicConfig(level=logging.INFO)
        logger.info("Using standard Python logging")

    session_service = DatabaseSessionService(db_url="sqlite:///sessions.db")
    artifact_service = InMemoryArtifactService()

    # Check for GCS artifact storage
    logs_bucket = os.getenv("LOGS_BUCKET_NAME")
    if logs_bucket:
        try:
            from google.adk.artifacts import GcsArtifactService
            artifact_service = GcsArtifactService(bucket_name=logs_bucket)
            logger.info("Using GCS artifact storage: %s", logs_bucket)
        except Exception as e:
            logger.warning("GCS artifact service unavailable, using in-memory: %s", e)

    app = get_fast_api_app(
        agent=root_agent,
        session_service=session_service,
        artifact_service=artifact_service,
    )

    @app.post("/feedback")
    async def collect_feedback(feedback: Feedback):
        logger.info("Feedback received: score=%s, text=%s", feedback.score, feedback.text)
        return {"status": "ok"}

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
