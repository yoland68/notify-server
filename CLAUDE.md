# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py

# Run with custom config
NOTIFY_HOST=0.0.0.0 NOTIFY_PORT=8765 NOTIFY_TOKEN=secret python server.py

# Send a test notification (while server is running)
curl -X POST http://localhost:8765/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "Test", "message": "Hello world"}'

# Check health
curl http://localhost:8765/health
```

## Architecture

`server.py` is the entire application — a single-file FastAPI + Tkinter app.

**Threading model:** The FastAPI/uvicorn server runs in a background daemon thread; the main thread runs the Tkinter event loop. Notifications cross the thread boundary via a `queue.Queue`, which the main thread polls every 100ms.

**Key components:**
- `Notification` — Pydantic model validating incoming POST payloads (`title`, `message`, `duration_ms`)
- `ToastManager` — creates and stacks Tkinter toast windows in the top-right corner; handles click-to-dismiss and auto-expiry
- `POST /notify` — enqueues a notification; optionally checks `X-Auth-Token` header against `NOTIFY_TOKEN` env var
- `GET /health` — returns `{"status": "ok"}`

**Environment variables:**
- `NOTIFY_HOST` — bind address (default: `0.0.0.0`)
- `NOTIFY_PORT` — port (default: `8765`)
- `NOTIFY_TOKEN` — if set, requests must include `X-Auth-Token: <token>`
