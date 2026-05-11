# notify-server

A Tailscale-reachable toast notification server for macOS. Send HTTP POST requests from any machine on your Tailscale network and see desktop notifications pop up in the top-right corner of your screen.

## Requirements

- macOS
- Python 3.10+
- Tailscale (optional, but the intended use case)

## Setup

```bash
pip install -r requirements.txt
python server.py
```

The server starts on `0.0.0.0:8765` by default and prints the local addresses it's reachable on.

## Configuration

All configuration is done via environment variables:

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_HOST` | `0.0.0.0` | Bind address |
| `NOTIFY_PORT` | `8765` | Listen port |
| `NOTIFY_TOKEN` | _(unset)_ | If set, requests must include `X-Auth-Token: <value>` |

Example with auth enabled:

```bash
NOTIFY_TOKEN=mysecret python server.py
```

## API

### `POST /notify`

Send a notification.

**Headers:**
- `Content-Type: application/json`
- `X-Auth-Token: <token>` _(required only if `NOTIFY_TOKEN` is set)_

**Body:**

```json
{
  "title": "Build finished",
  "message": "All tests passed.",
  "duration_ms": 6000
}
```

| Field | Type | Required | Constraints | Default |
|---|---|---|---|---|
| `title` | string | yes | 1–200 chars | — |
| `message` | string | no | max 2000 chars | `""` |
| `duration_ms` | integer | no | 500–60000 | 6000 |

**Response:**

```json
{"status": "queued"}
```

**Error responses:**
- `401` — missing or invalid `X-Auth-Token`
- `422` — validation error (field out of range, etc.)

---

### `GET /health`

```json
{"status": "ok"}
```

## Usage Examples

```bash
# Basic notification
curl -X POST http://<tailscale-ip>:8765/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "Hello", "message": "From another machine"}'

# With auth token
curl -X POST http://<tailscale-ip>:8765/notify \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: mysecret" \
  -d '{"title": "Deploy complete", "message": "v2.3.1 is live", "duration_ms": 10000}'

# From a shell script after a long job
my-long-build-command && curl -s -X POST http://notify:8765/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "Build done", "message": "$(date)"}'
```

## How It Works

The server runs two concurrent components:

1. **FastAPI/uvicorn** in a background thread — handles HTTP requests and pushes notification payloads into a thread-safe queue.
2. **Tkinter** on the main thread — polls the queue every 100ms and renders toast windows stacked in the top-right corner of the screen.

Toasts are click-to-dismiss and auto-close after `duration_ms`. Multiple toasts stack vertically and reflow when one is dismissed.

## Notifications appearance

Toasts use a dark Catppuccin-inspired theme:
- Background: `#1e1e2e`
- Title: `#f5e0dc` (bold, 13pt Helvetica)
- Message: `#cdd6f4` (11pt Helvetica)
- Width: 360px, positioned 18px from the top-right edge
