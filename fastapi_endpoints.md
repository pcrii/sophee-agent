# Sophee Agent FastAPI Endpoints

This document serves as a reference for all the custom FastAPI endpoints exposed by the Sophee agent backend. The server typically runs on the Pi at `http://192.168.1.225:8000`.

---

## Chat & Artifacts

### `POST /api/chat`
Send a message to the agent and receive a text response along with any newly generated artifact keys.
**Request Payload (JSON):**
```json
{
  "user_id": "string",
  "session_id": "string",
  "message": "string"
}
```
**Response (JSON):**
```json
{
  "status": "success",
  "response": "Agent's reply...",
  "artifacts": ["new_artifact_1.md"]
}
```

### `GET /api/artifacts/{user_id}/{session_id}/{filename}`
Retrieves the raw bytes of an artifact.
**Response:** Raw file bytes with the appropriate `Content-Type` (e.g., `image/png`, `text/markdown`, `audio/wav`).

---

## State & Data

### `GET /api/suggestions`
Returns the raw string contents of the `data/suggestion_box.md` file.
**Response (JSON):**
```json
{
  "status": "success",
  "contents": "## Scraped 2026-06-23..."
}
```

### `POST /api/suggestions/update`
Overwrites the contents of the `data/suggestion_box.md` file with the provided string.
**Request Payload (JSON):**
```json
{
  "contents": "string"
}
```
**Response (JSON):**
```json
{
  "status": "success",
  "message": "Suggestions file updated successfully."
}
```

### `GET /api/favorites`
Returns the structured favorites data from `data/user_favorites.json`.
**Response (JSON):**
```json
{
  "status": "success",
  "favorites": {
    "user_1": ["track_id_1", "track_id_2"]
  }
}
```

---

## Debugging

### `GET /api/debug/sessions`
Lists the 20 most recently updated conversation sessions from the database.
**Response (JSON):**
```json
{
  "status": "success",
  "sessions": [
    {
      "app_name": "app",
      "user_id": "philo",
      "session_id": "12345",
      "update_time": "2026-06-26 12:00:00"
    }
  ]
}
```

### `GET /api/debug/session/{user_id}/{session_id}`
Returns the full chronological list of events for the specified session, including payloads and LLM responses.
**Response (JSON):**
```json
{
  "status": "success",
  "events": [
    {
      "id": 1,
      "invocation_id": "abc-123",
      "timestamp": "2026-06-26 12:00:00",
      "event_data": {} // JSON payload of the event
    }
  ]
}
```

### `GET /api/debug/last_image_payload`
Returns the debug information (JSON) for the last image generation payload. Useful for inspecting exactly what was sent to the image model.
**Response (JSON):**
```json
{
  "status": "success",
  "payload": {
    "prompt": "...",
    "negative_prompt": "..."
  }
}
```

### `GET /api/debug/logs`
Returns the last N lines of the systemd `sophee.service` logs using `journalctl`.
**Query Parameters:**
- `lines` (optional, default: 100): Number of log lines to return. Example: `/api/debug/logs?lines=500`

**Response (JSON):**
```json
{
  "status": "success",
  "stdout": "Jul 26 12:00:01 sophee systemd[1]: Started Sophee Service...",
  "stderr": "",
  "exit_code": 0
}
```
