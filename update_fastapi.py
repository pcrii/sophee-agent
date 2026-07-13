import os
import re
import sys

with open('app/fast_api_app.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Replace FastAPI initialization to remove global dependencies
text = text.replace(
    'app = FastAPI(title="Sophee Agent API", dependencies=[Depends(verify_api_key)])',
    'app = FastAPI(title="Sophee Agent API")\n\n    from fastapi import APIRouter\n    api_router = APIRouter(dependencies=[Depends(verify_api_key)])'
)

# Replace all @app. routes with @api_router. routes for API endpoints
text = text.replace('@app.post("/api/chat")', '@api_router.post("/api/chat")')
text = text.replace('@app.get("/api/artifacts/', '@api_router.get("/api/artifacts/')
text = text.replace('@app.get("/api/suggestions")', '@api_router.get("/api/suggestions")')
text = text.replace('@app.post("/api/suggestions/update")', '@api_router.post("/api/suggestions/update")')
text = text.replace('@app.get("/api/favorites")', '@api_router.get("/api/favorites")')
text = text.replace('@app.get("/api/debug/sessions")', '@api_router.get("/api/debug/sessions")')
text = text.replace('@app.get("/api/debug/session/', '@api_router.get("/api/debug/session/')
text = text.replace('@app.get("/api/debug/last_image_payload")', '@api_router.get("/api/debug/last_image_payload")')
text = text.replace('@app.get("/api/debug/logs")', '@api_router.get("/api/debug/logs")')

# Inject router inclusion and static files mount at the end of create_app
mount_code = '''
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
'''

text = text.replace('    return app', mount_code)

with open('app/fast_api_app.py', 'w', encoding='utf-8') as f:
    f.write(text)
