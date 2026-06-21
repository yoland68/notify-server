# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (on backend host and every client)
pip install -r requirements.txt

# Run the backend (router; no GUI) — once, on an always-on host
python server.py
NOTIFY_HOST=0.0.0.0 NOTIFY_PORT=8766 NOTIFY_TOKEN=secret python server.py

# Run a client (renderer) on each Mac that should show toasts
NOTIFY_CLIENT=macbook NOTIFY_BACKEND=ws://<backend-host>:8766 python client.py

# Push a notification to a client (target is required)
curl -X POST http://localhost:8766/notify \
  -H "Content-Type: application/json" \
  -d '{"target": "macbook", "title": "Test", "message": "Hello world"}'

# Broadcast to all clients
curl -X POST http://localhost:8766/notify \
  -H "Content-Type: application/json" -d '{"target": "*", "title": "All hands"}'

# Persistent toast (stays until cleared/clicked), then clear it
curl -X POST http://localhost:8766/notify \
  -H "Content-Type: application/json" \
  -d '{"target": "macbook", "title": "Working", "persistent": true, "id": "job-1"}'
curl -X POST http://localhost:8766/notify/clear \
  -H "Content-Type: application/json" -d '{"target": "macbook", "id": "job-1"}'

# Inspect connected clients / offline queues
curl http://localhost:8766/clients

# Health
curl http://localhost:8766/health

# Claim the screen lock (blocks until granted), then release it
TOKEN=$(curl -s -X POST http://localhost:8766/lock/acquire \
  -H "Content-Type: application/json" \
  -d '{"client": "agent-1", "ttl_ms": 30000}' | jq -r .token)
curl -X POST http://localhost:8766/lock/release \
  -H "Content-Type: application/json" -d "{\"token\": \"$TOKEN\"}"
```

## Architecture

Two programs: **`server.py`** (central backend / router, no GUI) and **`client.py`** (per-Mac
renderer agent). The backend routes notifications to clients over WebSockets and coordinates a
global screen lock; clients connect, receive pushed messages, and render desktop toasts.

### Backend — `server.py`

Runs entirely on uvicorn's asyncio loop (no threads, no Tkinter). Started with `uvicorn.run`.

- `Hub` — the routing core. Holds `client id → set[WebSocket]` (`_conns`) and a bounded, TTL'd
  per-client offline queue (`_pending`), guarded by an `asyncio.Lock`. `deliver()` sends to all
  of a target's live sockets; if the target is offline it drops the message unless
  `queue_offline=True`, in which case it enqueues. `register()` flushes the queue on (re)connect;
  `broadcast()` hits every connected socket (never queues); `_safe_send` swallows dead-socket errors.
- `WS /ws?client=<id>[&token=]` — registers a connection (auth + client id from query params),
  then ignores inbound frames (push-only) until disconnect, when it unregisters.
- `NotifyRequest` / `POST /notify` — validated payload (`target`, `title`, `message`,
  `duration_ms`, `persistent`, `id`, `queue_offline`). Assigns a globally-unique `id` if none
  supplied (reusing an id replaces the toast in place on the client), builds a
  `{"type":"notify", ...}` message, and routes it. An offline target is dropped unless
  `queue_offline` is true. `target: "*"` broadcasts.
- `ClearRequest` / `POST /notify/clear` — sends `{"type":"clear", "id"|"all"}` to the target
  (live only — offline clients have nothing to clear). `422` if neither `id` nor `all`.
- `GET /clients` — connected clients (socket counts) and per-client pending counts.
- `Lease` / `ScreenLock` — async screen-lock manager: current `Lease` (client, fencing `token`,
  `acquired_at`, `expires_at`) + a FIFO `deque` of waiters parked on `asyncio.Future`s under an
  `asyncio.Lock`. `watchdog()` (started via `lifespan`) expires abandoned leases and promotes the
  next waiter, preventing deadlock if a holder crashes. Acquire/release/promote are race-safe (a
  timed-out waiter is skipped on promotion). Notifications track holder state via two helpers:
  `_grant_locked` calls `_lock_notify_held` (a **persistent** toast, stable id `screen-lock`) and
  the drain tail of `_promote_locked` calls `_lock_notify_free` (clear). So holding the lock pins a
  "🔒 Screen locked" toast on `NOTIFY_LOCK_TARGET` (default `mainbook`, `*` for all), repins it on
  hand-off, and clears it on release/expiry, when `NOTIFY_ON_LOCK` is on. The lock is **advisory**
  and **global** (one shared screen abstraction across all clients) — the backend never surfaces
  windows or clicks.
- `check_auth` — dependency enforcing `X-Auth-Token` on `/notify`, `/notify/clear`, `/clients`,
  and all `/lock/*` (not `/health`). WS auth uses `?token=`.
- Lock endpoints: `POST /lock/acquire` (blocks, FIFO, `408` on `wait_timeout_ms`; returns a
  fencing `token`), `POST /lock/release` / `POST /lock/renew` (token-gated, `409` if not holder),
  `GET /lock/status`.

### Client — `client.py`

Mirrors the backend's previous threading model: a background thread runs the asyncio WebSocket
client (`ws_loop`, reconnects on drop) and feeds received `notify`/`clear` messages into two
thread-safe queues (`notification_queue`, `clear_queue`); the main thread runs Tkinter and polls
those queues every 100ms.

- `ToastManager` — creates/stacks toast windows top-right; click-to-dismiss and auto-expiry.
  Tracks toasts by `id` in `self.by_id`; `persistent` toasts get no `after()` timer;
  `clear(id)` / `clear_all()` dismiss programmatically; reusing an id replaces in place.

### Environment variables

Backend: `NOTIFY_HOST` (`0.0.0.0`), `NOTIFY_PORT` (`8766`), `NOTIFY_TOKEN` (auth; `X-Auth-Token`
header for HTTP and `?token=` for WS), `NOTIFY_ON_LOCK` (`1`; persistent lock toasts), `NOTIFY_LOCK_TARGET` (`mainbook`; lock-toast recipient, `*` = all),
`NOTIFY_QUEUE_TTL_S` (`3600`), `NOTIFY_QUEUE_MAX` (`100`).

Client: `NOTIFY_BACKEND` (`ws://localhost:8766`), `NOTIFY_CLIENT` (default: hostname),
`NOTIFY_TOKEN`.
