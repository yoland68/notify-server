# notify-server

A central **notification backend** for many Mac clients, plus a **screen-lock coordinator**.

- **Backend** (`server.py`) — a router with no GUI. Clients connect over a WebSocket; any
  client or process pushes a notification to any client by POSTing to `/notify` with a
  `target`. The backend assigns a globally-unique id and delivers it over the target's live
  socket. If the target is offline the message is **dropped by default**; pass
  `queue_offline: true` to require delivery — it's buffered and flushed when the target reconnects.
- **Client agent** (`client.py`) — runs on each Mac. Connects to the backend with a client id
  and renders received notifications as desktop toasts in the top-right corner. Reconnects
  automatically.
- **Screen lock** — a global, lease-based advisory mutex so multiple agents/processes doing
  *computer use* (surfacing windows, clicking) don't fight over the screen. The backend never
  touches a screen itself; it only serializes cooperating clients.

```
   any process            ┌──────────────────────┐         ┌─ client.py "macmini"  → toasts
   POST /notify  ───────▶ │  server.py (backend) │ ──ws──▶ ├─ client.py "macbook"  → toasts
   {target, title, ...}   │  router + lock + hub │         └─ client.py "studio"   → toasts
                          └──────────────────────┘   (offline targets are queued, flushed on reconnect)
```

## Requirements

- macOS (for clients — they render with Tkinter)
- Python 3.10+
- Tailscale (optional, but the intended way to reach the backend across machines)

## Setup

Install deps on every machine:

```bash
pip install -r requirements.txt
```

**Run the backend** (once, on an always-on host — e.g. the mac mini):

```bash
python server.py
```

It listens on `0.0.0.0:8766` by default and prints the WebSocket URL clients should use.

**Run a client** on each Mac that should show toasts:

```bash
NOTIFY_CLIENT=macbook NOTIFY_BACKEND=ws://<backend-host>:8766 python client.py
```

The backend host can also run its own client (the two are separate processes).

## Configuration

**Backend** (`server.py`):

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_HOST` | `0.0.0.0` | Bind address |
| `NOTIFY_PORT` | `8766` | Listen port |
| `NOTIFY_TOKEN` | _(unset)_ | If set, HTTP requests need `X-Auth-Token: <value>` and WS connects need `?token=<value>` |
| `NOTIFY_ON_LOCK` | `1` | Pin a persistent "🔒 Screen locked" toast while the screen lock is held, cleared on release/expiry. `0` to disable. |
| `NOTIFY_LOCK_TARGET` | `mainbook` | Client that receives the persistent lock toast — the machine whose screen the automation drives. `*` to pin it on every connected client. |
| `NOTIFY_QUEUE_TTL_S` | `3600` | How long an offline client's queued messages live |
| `NOTIFY_QUEUE_MAX` | `100` | Max queued messages per offline client (oldest dropped) |

**Client** (`client.py`):

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_BACKEND` | `ws://localhost:8766` | Backend WebSocket base URL |
| `NOTIFY_CLIENT` | _(hostname)_ | This client's id — the `target` others address |
| `NOTIFY_TOKEN` | _(unset)_ | Auth token, if the backend requires one |

## API

### `POST /notify`

Push a notification to a client. The backend assigns a globally-unique `id` (or uses the one you
supply) and returns it.

**Headers:** `Content-Type: application/json`, plus `X-Auth-Token` if `NOTIFY_TOKEN` is set.

| Field | Type | Required | Constraints | Default |
|---|---|---|---|---|
| `target` | string | yes | client id, or `"*"` to broadcast | — |
| `title` | string | yes | 1–200 chars | — |
| `message` | string | no | max 2000 chars | `""` |
| `duration_ms` | integer | no | 500–60000 | 6000 |
| `persistent` | boolean | no | stays until cleared/clicked (ignores `duration_ms`) | `false` |
| `id` | string | no | 1–200 chars; reuse to replace a toast in place | _(generated)_ |
| `queue_offline` | boolean | no | if target offline: `true` queues until reconnect, `false` drops | `false` |

**Response:**

```json
{"id": "ab12…", "target": "macbook", "delivered": true, "queued": false}
```

- `delivered` — sent to at least one live socket. `queued` — buffered for an offline target.
- By default an offline target is **dropped** (`delivered: false, queued: false`). Set
  `queue_offline: true` to buffer it instead and deliver on reconnect.
- Broadcast (`target: "*"`) responses include `"broadcast": true` and only reach connected clients.

### `POST /notify/clear`

Dismiss a notification on a client (e.g. when a long-running job finishes).

| Field | Type | Description |
|---|---|---|
| `target` | string | client id, or `"*"` to clear on all clients |
| `id` | string | clear the toast with this id |
| `all` | boolean | clear every toast on the target |

Provide exactly one of `id` / `all`. Returns `{"status": "clearing", ...}`. Clears are only sent
to live clients (an offline client has nothing to clear). `422` if neither `id` nor `all` given.

### `GET /clients`

```json
{"connected": {"macbook": 1, "macmini": 1}, "pending": {"studio": 2}}
```

Connected clients (with socket counts) and per-client queued-message counts.

### `WS /ws?client=<id>[&token=<token>]`

Client connection. On connect, any queued messages for `<id>` are flushed. The server pushes
JSON messages: `{"type": "notify", "id", "title", "message", "duration_ms", "persistent"}` and
`{"type": "clear", "id"|"all"}`. Inbound messages from the client are ignored (the socket is
push-only); `client.py` manages it for you.

### Usage examples

```bash
# Notify one client
curl -X POST http://<backend>:8766/notify \
  -H "Content-Type: application/json" \
  -d '{"target": "macbook", "title": "Hello", "message": "from the mac mini"}'

# Broadcast to everyone
curl -X POST http://<backend>:8766/notify \
  -H "Content-Type: application/json" \
  -d '{"target": "*", "title": "Deploy starting"}'

# Persistent status toast that you clear when the job is done
curl -s -X POST http://<backend>:8766/notify \
  -H "Content-Type: application/json" \
  -d '{"target": "macbook", "title": "Deploying…", "persistent": true, "id": "deploy"}'
./deploy.sh
curl -s -X POST http://<backend>:8766/notify/clear \
  -H "Content-Type: application/json" \
  -d '{"target": "macbook", "id": "deploy"}'
```

---

## Screen lock

A single global, lease-based mutex over the screen. Exactly one client holds it at a time;
others **block** on `acquire` (long-poll) and are granted the lock in **FIFO order** when it
frees up. Every grant returns a fencing `token` required to `release` or `renew` — so a stale
holder (whose lease already expired and was handed to someone else) can never release or extend
someone else's lock. A watchdog auto-expires abandoned leases, so a crashed holder can't
deadlock everyone. When `NOTIFY_ON_LOCK` is on, holding the lock pins a **persistent** "🔒 Screen
locked" toast on `NOTIFY_LOCK_TARGET` (default `mainbook` — the client whose screen the automation
drives) for as long as it's held; the toast (stable id `screen-lock`) is repinned for the new
holder on hand-off and cleared on release or lease expiry, so that machine always shows whether
its screen is currently under automation.

> Note: this is a single *global* lock across all clients (one shared "screen" abstraction),
> not one lock per machine. Revisit if you need per-client locks.

All lock endpoints honor `X-Auth-Token` when `NOTIFY_TOKEN` is set.

### Protocol

1. `POST /lock/acquire {"client": "...", "ttl_ms": ...}` — **blocks** until granted (or times
   out). On `200` you hold the screen until `expires_at`.
2. Do your computer use. For jobs longer than `ttl_ms`, `POST /lock/renew` before expiry.
3. `POST /lock/release {"token": "..."}` when done — or just let the lease expire.
4. Other clients' parked `acquire` calls unblock in FIFO order.

### `POST /lock/acquire`

| Field | Type | Required | Constraints | Default |
|---|---|---|---|---|
| `client` | string | yes | 1–200 chars | — |
| `ttl_ms` | integer | no | 500–3600000 | 30000 |
| `wait_timeout_ms` | integer | no | 0–3600000 | 60000 |

**Response `200`:** `{"token": "ab12…", "holder": "agent-1", "acquired_at": …, "expires_at": …}`
— `408` if `wait_timeout_ms` elapses first (use `0` for a non-blocking try).

### `POST /lock/release`

`{"token": "ab12…"}` → `{"released": true}`; `409` if the token isn't the current holder.

### `POST /lock/renew`

`{"token": "ab12…", "ttl_ms": 30000}` → `{"expires_at": …}`; `409` if not the current holder.

### `GET /lock/status`

```json
{"locked": true, "holder": "agent-1", "acquired_at": …, "expires_at": …,
 "queue_depth": 2, "waiters": ["agent-2", "agent-3"]}
```

---

### `GET /health`

```json
{"status": "ok"}
```

## How It Works

**Backend** (`server.py`) runs entirely on uvicorn's asyncio loop — no GUI:

- A `Hub` holds `client id → set of live WebSockets` and a bounded, TTL'd per-client queue for
  offline targets. `POST /notify` routes to the target's sockets; if the target is offline it
  drops the message, or enqueues it when `queue_offline` is set. `/ws` flushes the queue on connect.
- The screen lock is an async manager: current lease + a FIFO queue of waiters parked on
  `asyncio` futures, plus a watchdog task that expires abandoned leases and promotes the next
  waiter.

**Client** (`client.py`) mirrors the backend's old threading model: a background thread runs the
asyncio WebSocket client (reconnecting on drop) and feeds received messages into two thread-safe
queues; the main thread runs Tkinter and polls those queues every 100ms to render/clear toasts.

Toasts are click-to-dismiss and auto-close after `duration_ms`; `persistent` toasts skip the
timer and are tracked by `id` so they can be cleared later or replaced in place by posting again
with the same `id`. Multiple toasts stack vertically and reflow when one is dismissed.

## Notifications appearance

Toasts use a dark Catppuccin-inspired theme (configured in `client.py`):
- Background: `#1e1e2e`
- Title: `#f5e0dc` (bold, 13pt Helvetica)
- Message: `#cdd6f4` (11pt Helvetica)
- Width: 360px, positioned 18px from the top-right edge
