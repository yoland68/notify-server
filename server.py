#!/usr/bin/env python3
"""Central notification backend for many Mac clients.

Clients connect over a WebSocket (``/ws?client=<id>``) and render toasts
locally (see ``client.py``). Any client or process can push a notification to
any client by POSTing to ``/notify`` with a ``target``; the backend assigns a
globally-unique id and delivers it over the target's live socket, or queues it
for delivery when the target reconnects.

  POST /notify        {"target": "...", "title": "...", "message": "...", ...}
  POST /notify/clear  {"target": "...", "id": "..."} | {"target": "...", "all": true}
  GET  /clients       connected clients and queued counts
  WS   /ws?client=ID  client connection (receives notify/clear messages)
  GET  /health
  + screen-lock coordination endpoints (POST /lock/acquire, ...)
"""

import asyncio
import os
import socket
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field


HOST = os.environ.get("NOTIFY_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOTIFY_PORT", "8766"))
AUTH_TOKEN = os.environ.get("NOTIFY_TOKEN") or None

# Offline delivery: per-client queue bounds.
PENDING_TTL_S = float(os.environ.get("NOTIFY_QUEUE_TTL_S", "3600"))
PENDING_MAXLEN = int(os.environ.get("NOTIFY_QUEUE_MAX", "100"))

LOCK_DEFAULT_TTL_MS = 30000
LOCK_DEFAULT_WAIT_MS = 60000
LOCK_WATCHDOG_INTERVAL_S = 0.25
NOTIFY_ON_LOCK = os.environ.get("NOTIFY_ON_LOCK", "1").lower() not in ("0", "false", "no")

# Where the "screen locked" toast goes — the mac mini, whose screen the lock
# guards. Set to "*" to pin it on every connected client instead.
LOCK_NOTIFY_TARGET = os.environ.get("NOTIFY_LOCK_TARGET", "macmini")
# Stable id so acquire shows one toast and release clears that exact toast; reusing
# the id also lets a hand-off replace the holder text in place.
LOCK_NOTIFY_ID = "screen-lock"

# Reserved target meaning "every connected client".
BROADCAST = "*"


class NotifyRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=200)
    message: str = Field("", max_length=2000)
    duration_ms: Optional[int] = Field(None, ge=500, le=60000)
    # When true the toast never auto-expires; it stays until cleared or clicked.
    persistent: bool = False
    # Optional caller-supplied id. Reusing an id replaces the toast in place on
    # the client. If omitted, a globally-unique id is generated and returned.
    id: Optional[str] = Field(None, min_length=1, max_length=200)
    # By default a notification to an offline client is dropped. Set this to
    # require delivery: the message is queued and delivered when the client
    # (re)connects.
    queue_offline: bool = False


class ClearRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=200)
    id: Optional[str] = Field(None, min_length=1, max_length=200)
    all: bool = False


class AcquireRequest(BaseModel):
    client: str = Field(..., min_length=1, max_length=200)
    ttl_ms: int = Field(LOCK_DEFAULT_TTL_MS, ge=500, le=3_600_000)
    wait_timeout_ms: int = Field(LOCK_DEFAULT_WAIT_MS, ge=0, le=3_600_000)


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


class RenewRequest(BaseModel):
    token: str = Field(..., min_length=1)
    ttl_ms: int = Field(LOCK_DEFAULT_TTL_MS, ge=500, le=3_600_000)


class Hub:
    """Routes notifications to connected clients; queues for offline ones.

    A client may have more than one live socket (e.g. it reconnected before the
    old socket dropped); messages go to all of them. When a client has no live
    socket, deliveries are buffered in a bounded, TTL'd per-client queue and
    flushed on the next connect.
    """

    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = {}
        self._pending: dict[str, "deque[tuple[float, dict]]"] = {}
        self._lock = asyncio.Lock()

    async def register(self, client: str, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.setdefault(client, set()).add(ws)
            pending = self._pending.pop(client, None)
        if pending:
            now = time.time()
            for expires_at, msg in pending:
                if expires_at >= now:
                    await self._safe_send(ws, msg)

    async def unregister(self, client: str, ws: WebSocket) -> None:
        async with self._lock:
            socks = self._conns.get(client)
            if socks is not None:
                socks.discard(ws)
                if not socks:
                    self._conns.pop(client, None)

    async def deliver(self, target: str, msg: dict, queue_offline: bool = False) -> dict:
        async with self._lock:
            socks = list(self._conns.get(target, ()))
        if socks:
            for ws in socks:
                await self._safe_send(ws, msg)
            return {"delivered": True, "queued": False}
        if queue_offline:
            async with self._lock:
                dq = self._pending.setdefault(target, deque(maxlen=PENDING_MAXLEN))
                dq.append((time.time() + PENDING_TTL_S, msg))
            return {"delivered": False, "queued": True}
        return {"delivered": False, "queued": False}

    async def broadcast(self, msg: dict) -> dict:
        async with self._lock:
            socks = [ws for socket_set in self._conns.values() for ws in socket_set]
        for ws in socks:
            await self._safe_send(ws, msg)
        return {"delivered": bool(socks), "queued": False}

    async def clients(self) -> dict:
        async with self._lock:
            return {
                "connected": {c: len(s) for c, s in self._conns.items()},
                "pending": {c: len(q) for c, q in self._pending.items()},
            }

    @staticmethod
    async def _safe_send(ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass  # socket is dead; unregister happens on its own receive loop


hub = Hub()


def _route_lock_notification(msg: dict) -> None:
    """Fire-and-forget a lock notify/clear to the configured target.

    Best-effort: never queued for an offline client, so a dropped mac mini can't
    receive a stale 'locked' toast minutes later with no matching clear.
    """
    if not NOTIFY_ON_LOCK:
        return
    if LOCK_NOTIFY_TARGET == BROADCAST:
        asyncio.create_task(hub.broadcast(msg))
    else:
        asyncio.create_task(hub.deliver(LOCK_NOTIFY_TARGET, msg))


def _lock_held_msg(client: str, duration_ms: float) -> dict:
    """A 'screen locked' toast that **auto-expires with the lease**.

    Deliberately NOT `persistent`: a truly persistent toast outlives any lost
    `clear` (server restart, or the target offline at release) and strands a 🔒
    on screen forever — which looks like the lock is stuck. Tying its lifetime to
    the lease TTL bounds that: the toast can never outlive the lock by more than
    one TTL, and while the lock is genuinely held it's refreshed on every renew
    (reusing the id resets the client-side timer), so it stays up the whole time.
    """
    return {
        "type": "notify",
        "id": LOCK_NOTIFY_ID,
        "title": "🔒 Screen locked",
        "message": f"{client} is controlling the screen",
        "duration_ms": max(1, int(duration_ms)),
        "persistent": False,
    }


def _lock_clear_msg() -> dict:
    return {"type": "clear", "id": LOCK_NOTIFY_ID, "all": False}


def _lock_notify_held(client: str, ttl_ms: float) -> None:
    _route_lock_notification(_lock_held_msg(client, ttl_ms))


def _lock_notify_free() -> None:
    _route_lock_notification(_lock_clear_msg())


@dataclass
class Lease:
    """An exclusive hold on the screen, owned by one client until expiry."""

    client: str
    token: str
    acquired_at: float
    expires_at: float


class ScreenLock:
    """A single global, lease-based mutex over the screen.

    The lock is *advisory*: it serializes cooperating clients but performs no
    screen actions itself. Each grant carries a fencing ``token`` required to
    release or renew, so a stale holder (whose lease already expired and was
    reassigned) can never release or extend someone else's lock. A watchdog
    expires abandoned leases so a crashed holder can't deadlock everyone.
    """

    def __init__(self) -> None:
        self._holder: Optional[Lease] = None
        self._waiters: "deque[tuple[str, float, asyncio.Future]]" = deque()
        self._mutex = asyncio.Lock()

    def _grant_locked(self, client: str, ttl_ms: float) -> Lease:
        now = time.time()
        lease = Lease(client, uuid.uuid4().hex, now, now + ttl_ms / 1000.0)
        self._holder = lease
        _lock_notify_held(client, ttl_ms)
        return lease

    def _reap_locked(self) -> None:
        """Drop an expired holder and hand off to the next waiter."""
        if self._holder is not None and time.time() >= self._holder.expires_at:
            self._holder = None
            self._promote_locked()

    def _promote_locked(self) -> None:
        """Give the lock to the next live waiter, or leave it free.

        Notifications follow the holder: a grant repins the lock toast for the new
        holder (`_grant_locked`); draining to no holder clears it.
        """
        while self._waiters:
            client, ttl_ms, fut = self._waiters.popleft()
            if fut.cancelled() or fut.done():
                continue  # waiter gave up (timed out); skip it
            fut.set_result(self._grant_locked(client, ttl_ms))
            return
        self._holder = None
        _lock_notify_free()

    async def acquire(self, client: str, ttl_ms: float, wait_timeout_ms: float) -> Lease:
        loop = asyncio.get_running_loop()
        async with self._mutex:
            self._reap_locked()
            if self._holder is None:
                return self._grant_locked(client, ttl_ms)
            fut: asyncio.Future = loop.create_future()
            entry = (client, ttl_ms, fut)
            self._waiters.append(entry)

        try:
            return await asyncio.wait_for(fut, wait_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            async with self._mutex:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    # Promoted at the moment of timeout: honor the grant.
                    if fut.done() and not fut.cancelled():
                        return fut.result()
            raise HTTPException(status_code=408, detail="timeout waiting for lock")

    async def release(self, token: str) -> bool:
        async with self._mutex:
            if self._holder is None or self._holder.token != token:
                return False
            self._holder = None
            self._promote_locked()
            return True

    async def renew(self, token: str, ttl_ms: float) -> Optional[Lease]:
        async with self._mutex:
            self._reap_locked()
            if self._holder is None or self._holder.token != token:
                return None
            self._holder.expires_at = time.time() + ttl_ms / 1000.0
            _lock_notify_held(self._holder.client, ttl_ms)  # refresh the toast timer
            return self._holder

    async def status(self) -> dict:
        async with self._mutex:
            self._reap_locked()
            h = self._holder
            return {
                "locked": h is not None,
                "holder": h.client if h else None,
                "acquired_at": h.acquired_at if h else None,
                "expires_at": h.expires_at if h else None,
                "queue_depth": len(self._waiters),
                "waiters": [c for c, _, _ in self._waiters],
            }

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(LOCK_WATCHDOG_INTERVAL_S)
            async with self._mutex:
                self._reap_locked()


lock = ScreenLock()


async def _reconcile_lock_toast(client: str, ws: WebSocket) -> None:
    """Bring a just-(re)connected client's lock toast in line with reality.

    Belt-and-suspenders against orphans: if a live lock targets this client,
    re-assert the toast with the lease's remaining time; otherwise clear any
    stale 🔒 it may still be showing from before it dropped or the server
    restarted. Clearing a non-existent toast is a harmless no-op, so this is
    always safe to send.
    """
    if not NOTIFY_ON_LOCK:
        return
    st = await lock.status()
    is_target = LOCK_NOTIFY_TARGET in (BROADCAST, client)
    if st["locked"] and is_target:
        remaining_ms = (st["expires_at"] - time.time()) * 1000.0
        await Hub._safe_send(ws, _lock_held_msg(st["holder"], remaining_ms))
    else:
        await Hub._safe_send(ws, _lock_clear_msg())


def check_auth(x_auth_token: Optional[str] = Header(default=None)) -> None:
    if AUTH_TOKEN and x_auth_token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(lock.watchdog())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    client = ws.query_params.get("client")
    token = ws.query_params.get("token")
    if not client or (AUTH_TOKEN and token != AUTH_TOKEN):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    await hub.register(client, ws)
    await _reconcile_lock_toast(client, ws)
    try:
        while True:
            await ws.receive_text()  # inbound is ignored; keeps the socket open
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(client, ws)


@app.post("/notify", dependencies=[Depends(check_auth)])
async def notify(n: NotifyRequest):
    nid = n.id or uuid.uuid4().hex
    msg = {
        "type": "notify",
        "id": nid,
        "title": n.title,
        "message": n.message,
        "duration_ms": n.duration_ms,
        "persistent": n.persistent,
    }
    if n.target == BROADCAST:
        result = await hub.broadcast(msg)
        result["broadcast"] = True
    else:
        result = await hub.deliver(n.target, msg, queue_offline=n.queue_offline)
    return {"id": nid, "target": n.target, **result}


@app.post("/notify/clear", dependencies=[Depends(check_auth)])
async def notify_clear(req: ClearRequest):
    if not req.all and not req.id:
        raise HTTPException(status_code=422, detail="provide 'id' or 'all'")
    msg = {"type": "clear", "id": req.id, "all": req.all}
    if req.target == BROADCAST:
        await hub.broadcast(msg)
        return {"status": "clearing", "broadcast": True}
    # Clearing only matters for a live client; don't queue for offline ones.
    result = await hub.deliver(req.target, msg, queue_offline=False)
    return {"status": "clearing", **result}


@app.get("/clients", dependencies=[Depends(check_auth)])
async def clients():
    return await hub.clients()


@app.post("/lock/acquire", dependencies=[Depends(check_auth)])
async def lock_acquire(req: AcquireRequest):
    lease = await lock.acquire(req.client, req.ttl_ms, req.wait_timeout_ms)
    return {
        "token": lease.token,
        "holder": lease.client,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
    }


@app.post("/lock/release", dependencies=[Depends(check_auth)])
async def lock_release(req: TokenRequest):
    if not await lock.release(req.token):
        raise HTTPException(status_code=409, detail="not the lock holder")
    return {"released": True}


@app.post("/lock/renew", dependencies=[Depends(check_auth)])
async def lock_renew(req: RenewRequest):
    lease = await lock.renew(req.token, req.ttl_ms)
    if lease is None:
        raise HTTPException(status_code=409, detail="not the lock holder")
    return {"expires_at": lease.expires_at}


@app.get("/lock/status", dependencies=[Depends(check_auth)])
async def lock_status():
    return await lock.status()


@app.get("/health")
def health():
    return {"status": "ok"}


def _local_ips() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except socket.gaierror:
        pass
    return ips


def main():
    print(f"Notification backend listening on http://{HOST}:{PORT}")
    for ip in _local_ips():
        print(f"  clients connect to ws://{ip}:{PORT}/ws?client=<id>")
    if AUTH_TOKEN:
        print("  auth: X-Auth-Token header (HTTP) / ?token= (WebSocket) required")
    if NOTIFY_ON_LOCK:
        dest = "all clients" if LOCK_NOTIFY_TARGET == BROADCAST else f"'{LOCK_NOTIFY_TARGET}'"
        print(f"  lock toasts: on -> {dest} (persistent; NOTIFY_ON_LOCK/NOTIFY_LOCK_TARGET)")
    else:
        print("  lock toasts: off (NOTIFY_ON_LOCK=0)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
