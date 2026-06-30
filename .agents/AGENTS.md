# Antigravity Rules for Sophee Codebase

These rules apply to the Antigravity agent (the AI coding assistant) when writing, editing, or refactoring code for this workspace:


- **DEPLOYMENT ENVIRONMENT**: The code is run on a Raspberry Pi with a FastAPI server running on it. Keep this environment in mind for performance, dependency management, and architecture.
- **REMOTE DATA**: The application runs live on the Pi (IP: `192.168.1.225`). Do NOT rely on local data files (e.g., `data/suggestion_box.md` or logs) for runtime state. Always fetch live data by using the `read_url_content` tool to hit the FastAPI endpoints (e.g., `http://192.168.1.225:8000/api/suggestions`, `/api/favorites`, `/api/debug/logs`, `/api/debug/sessions`).
- **GIT DEPLOYMENT**: Always run `git add`, `git commit`, and `git push` when you complete a feature or finish coding. The user never runs the script locally; they deploy it to the Raspberry Pi by pulling from Git, so the repository must always be updated.
