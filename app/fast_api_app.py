"""FastAPI server for the Sophee agent API."""

import logging
import os
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
import os

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

from fastapi import Query
async def verify_api_key(
    api_key_header: str = Security(api_key_header),
    api_key_query: str = Query(None, alias="api_key")
):
    expected_api_key = os.getenv("SOPHEE_API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Server API key not configured."
        )
    key_to_check = api_key_header or api_key_query
    if key_to_check != expected_api_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Invalid API Key"
        )

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
    from google.adk.artifacts import FileArtifactService
    from google.adk.sessions import DatabaseSessionService
    from google.adk.runners import Runner
    from app.agent import root_agent

    app = FastAPI(title="Sophee Agent API")

    from fastapi import APIRouter
    api_router = APIRouter(dependencies=[Depends(verify_api_key)])

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    artifacts_dir = os.path.join(project_root, "data", "artifacts")

    session_service = DatabaseSessionService(db_url="sqlite+aiosqlite:///sessions.db")
    artifact_service = FileArtifactService(root_dir=artifacts_dir)

    runner = Runner(
        agent=root_agent,
        app_name="app",
        session_service=session_service,
        artifact_service=artifact_service,
    )

    @api_router.post("/api/chat")
    async def chat(request: ChatRequest):
        """Send a message to the agent and get the text response."""
        from google.genai import types
        response_text = ""
        try:
            session = await session_service.get_session(app_name="app", user_id=request.user_id, session_id=request.session_id)
            if not session:
                await session_service.create_session(app_name="app", user_id=request.user_id, session_id=request.session_id)
                
            # Track artifacts before the run
            before_keys = set(
                await artifact_service.list_artifact_keys(
                    app_name="app", user_id=request.user_id, session_id=request.session_id
                )
            )

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
            
            # Check for new artifacts
            after_keys = set(
                await artifact_service.list_artifact_keys(
                    app_name="app", user_id=request.user_id, session_id=request.session_id
                )
            )
            new_keys = list(after_keys - before_keys)

            return {"status": "success", "response": response_text, "artifacts": new_keys}
        except Exception as e:
            logger.exception("Error during API chat invocation:")
            return {"status": "error", "message": str(e)}

    from fastapi.responses import Response

    @api_router.get("/api/artifacts/{user_id}/{session_id}/{filename}")
    async def get_artifact(user_id: str, session_id: str, filename: str):
        """Returns the raw bytes of an artifact."""
        try:
            part_data = await artifact_service.load_artifact(
                app_name="app",
                user_id=user_id,
                session_id=session_id,
                filename=filename,
            )
            mime_type = part_data.inline_data.mime_type
            if not mime_type:
                mime_type = "application/octet-stream"
            return Response(content=part_data.inline_data.data, media_type=mime_type)
        except Exception as e:
            logger.exception("Error during artifact retrieval:")
            return {"status": "error", "message": str(e)}


    @api_router.get("/api/suggestions")
    async def get_suggestions():
        """Returns the suggestions from the SQLite database."""
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db")
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, author, content, status FROM suggestions ORDER BY id ASC")
            rows = cursor.fetchall()
            conn.close()
            
            lines = []
            for row in rows:
                db_id, timestamp, author, content, status = row
                box = "[x]" if status == "DONE" else "[ ]"
                lines.append(f"- {box} **[{timestamp}]** {author} (ID: {db_id}): {content}")
                
            contents = "\\n".join(lines)
            return {"status": "success", "contents": contents}
        except sqlite3.OperationalError:
            return {"status": "info", "message": "No suggestions found.", "contents": ""}
        except Exception as e:
            return {"status": "error", "message": str(e)}


    @api_router.post("/api/suggestions/update")
    async def update_suggestions(request: SuggestionsUpdateRequest):
        """Deprecated. Use the agent tools to manage suggestion status."""
        return {"status": "error", "message": "Suggestions are now managed via SQLite. Please use the agent tools to mark them as DONE."}


    @api_router.get("/api/favorites")
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


    @api_router.get("/api/chat/sessions")
    async def get_chat_sessions():
        """Returns all chat sessions across all users."""
        import sqlite3
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sessions.db"
        )
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT app_name, user_id, id, update_time, state
                FROM sessions
                ORDER BY update_time DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            sessions_list = []
            for row in rows:
                sessions_list.append({
                    "app_name": row[0],
                    "user_id": row[1],
                    "session_id": row[2],
                    "update_time": row[3]
                })
            return {"status": "success", "sessions": sessions_list}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @api_router.get("/api/chat/history/{user_id}/{session_id}")
    async def get_chat_history(user_id: str, session_id: str):
        """Returns the chat history for a given session."""
        import sqlite3
        import json
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sessions.db"
        )
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, event_data
                FROM events
                WHERE user_id = ? AND session_id = ?
                ORDER BY timestamp ASC
            """, (user_id, session_id))
            rows = cursor.fetchall()
            conn.close()
            
            history = []
            for row in rows:
                timestamp, event_data_raw = row
                try:
                    event_data = json.loads(event_data_raw)
                except Exception:
                    continue
                
                author = event_data.get("author", "")
                content = event_data.get("content", {})
                parts = content.get("parts", [])
                text = "".join([p.get("text", "") for p in parts if "text" in p])
                
                if text:
                    if author == "user":
                        history.append({"sender": "user", "text": text, "artifacts": [], "payload": event_data})
                    elif author and author != "system":
                        history.append({"sender": "bot", "text": text, "artifacts": [], "payload": event_data})
            
            return {"status": "success", "history": history}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @api_router.get("/api/debug/sessions")
    async def list_debug_sessions():
        """Lists the most recently updated sessions from the database."""
        import sqlite3
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sessions.db"
        )
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT app_name, user_id, id, update_time
                FROM sessions
                ORDER BY update_time DESC
                LIMIT 20
            """)
            rows = cursor.fetchall()
            conn.close()
            sessions_list = [
                {
                    "app_name": row[0],
                    "user_id": row[1],
                    "session_id": row[2],
                    "update_time": row[3]
                }
                for row in rows
            ]
            return {"status": "success", "sessions": sessions_list}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @api_router.get("/api/debug/session/{user_id}/{session_id}")
    async def get_debug_session(user_id: str, session_id: str):
        """Returns the full list of events for the specified session."""
        import sqlite3
        import json
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sessions.db"
        )
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, invocation_id, timestamp, event_data
                FROM events
                WHERE user_id = ? AND session_id = ?
                ORDER BY timestamp ASC
            """, (user_id, session_id))
            rows = cursor.fetchall()
            conn.close()
            
            events_list = []
            for row in rows:
                try:
                    event_data = json.loads(row[3])
                except Exception:
                    event_data = row[3]
                events_list.append({
                    "id": row[0],
                    "invocation_id": row[1],
                    "timestamp": row[2],
                    "event_data": event_data
                })
            return {"status": "success", "events": events_list}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @api_router.get("/api/debug/last_image_payload")
    async def get_last_image_payload():
        """Returns the debug info for the last image generation payload."""
        payload_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "last_image_payload.json"
        )
        if not os.path.exists(payload_file):
            return {"status": "info", "message": "No payload found."}
        try:
            import json
            with open(payload_file, encoding="utf-8") as f:
                payload = json.load(f)
            return {"status": "success", "payload": payload}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @api_router.get("/api/debug/logs")
    async def get_debug_logs(lines: int = 100):
        """Returns the last N lines of the systemd service logs."""
        import subprocess
        try:
            res = subprocess.run(
                ["journalctl", "-u", "sophee.service", "-n", str(lines), "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return {
                "status": "success",
                "stdout": res.stdout,
                "stderr": res.stderr,
                "exit_code": res.returncode
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/debug/image-payload")
    async def get_last_image_payload():
        """Returns the last payload sent to the image generation API."""
        import json
        payload_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "last_image_payload.json")
        try:
            with open(payload_path) as f:
                return {"status": "success", "payload": json.load(f)}
        except FileNotFoundError:
            return {"status": "error", "message": "No image payload recorded yet."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/debug/last-image-out")
    async def get_last_image_out():
        """Returns the raw string output of the last interaction API call."""
        import os
        out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "last_image_out.json")
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                return {"status": "success", "content": f.read()}
        except FileNotFoundError:
            return {"status": "error", "message": "No output recorded yet."}
        except Exception as e:
            return {"status": "error", "message": str(e)}


    app.include_router(api_router)

    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import RedirectResponse
    static_dir = os.path.join(project_root, "static")
    os.makedirs(static_dir, exist_ok=True)
    
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def root():
        return RedirectResponse(url="/static/index.html")

    return app




app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
