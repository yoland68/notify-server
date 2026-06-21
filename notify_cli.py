#!/usr/bin/env python3
"""notify — CLI for the notification backend.

Send desktop-toast notifications to Mac clients through the central backend
(see server.py), and inspect clients / the screen lock. Standard library only,
so it runs anywhere Python 3 does — no pip install for the CLI itself.

Backend URL is taken from --url, then $NOTIFY_URL, then $NOTIFY_BACKEND (a
ws:// URL is rewritten to http://), else http://localhost:8766.
Auth token from --token, then $NOTIFY_TOKEN.

Examples:
  notify send macbook "Build done" "All tests passed"
  notify send macmini "Working…" --persistent --id job1
  notify clear macmini --id job1
  notify broadcast "Deploy starting"
  notify clients
  notify lock
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:8766"


def resolve_url(arg):
    url = arg or os.environ.get("NOTIFY_URL") or os.environ.get("NOTIFY_BACKEND") or DEFAULT_URL
    if url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    return url.rstrip("/")


def request(method, url, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except urllib.error.URLError as e:
        print(f"error: cannot reach backend at {url} ({e.reason})", file=sys.stderr)
        sys.exit(2)


def _err(status, resp):
    detail = resp.get("detail") if isinstance(resp, dict) else resp
    print(f"error: backend returned {status}: {detail}", file=sys.stderr)
    return 1


def cmd_send(args, url, token, *, target=None):
    body = {"target": target if target is not None else args.target, "title": args.title}
    if args.message:
        body["message"] = args.message
    if args.duration_ms is not None:
        body["duration_ms"] = args.duration_ms
    if args.persistent:
        body["persistent"] = True
    if args.id:
        body["id"] = args.id
    if getattr(args, "queue_offline", False):
        body["queue_offline"] = True
    status, resp = request("POST", url, "/notify", token, body)
    if args.json:
        print(json.dumps(resp, indent=2))
    elif status >= 400:
        return _err(status, resp)
    else:
        tid, tgt = resp.get("id", ""), resp.get("target", "")
        if resp.get("broadcast"):
            print(f"✓ broadcast to connected clients (id {tid})")
        elif resp.get("delivered"):
            print(f"✓ delivered to {tgt} (id {tid})")
        elif resp.get("queued"):
            print(f"• queued for {tgt} — offline, will deliver on reconnect (id {tid})")
        else:
            print(f"✗ dropped — {tgt} is offline (use --queue-offline to hold it) (id {tid})")
    return 0 if status < 400 else 1


def cmd_broadcast(args, url, token):
    return cmd_send(args, url, token, target="*")


def cmd_clear(args, url, token):
    if not args.all and not args.id:
        print("error: provide --id <id> or --all", file=sys.stderr)
        return 2
    body = {"target": args.target}
    if args.all:
        body["all"] = True
    else:
        body["id"] = args.id
    status, resp = request("POST", url, "/notify/clear", token, body)
    if args.json:
        print(json.dumps(resp, indent=2))
    elif status >= 400:
        return _err(status, resp)
    else:
        what = "all toasts" if args.all else f"toast {args.id}"
        print(f"✓ cleared {what} on {args.target}")
    return 0 if status < 400 else 1


def cmd_clients(args, url, token):
    status, resp = request("GET", url, "/clients", token)
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0 if status < 400 else 1
    if status >= 400:
        return _err(status, resp)
    connected = resp.get("connected", {})
    pending = resp.get("pending", {})
    if connected:
        print("connected:")
        for c, n in sorted(connected.items()):
            print(f"  {c}" + (f" ({n} sockets)" if n != 1 else ""))
    else:
        print("connected: (none)")
    if pending:
        print("pending (offline queues):")
        for c, n in sorted(pending.items()):
            print(f"  {c}: {n}")
    return 0


def cmd_lock(args, url, token):
    status, resp = request("GET", url, "/lock/status", token)
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0 if status < 400 else 1
    if status >= 400:
        return _err(status, resp)
    if resp.get("locked"):
        print(f"locked by {resp.get('holder')}")
        waiters = resp.get("waiters") or []
        if waiters:
            print("waiting:", ", ".join(waiters))
    else:
        print("unlocked")
    return 0


def cmd_health(args, url, token):
    status, resp = request("GET", url, "/health", token)
    print(json.dumps(resp) if not args.json else json.dumps(resp, indent=2))
    return 0 if status < 400 else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="notify",
        description="Send desktop notifications to Mac clients via the backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Backend: --url / $NOTIFY_URL / $NOTIFY_BACKEND (default http://localhost:8766).",
    )
    p.add_argument("--url", help="backend base URL")
    p.add_argument("--token", default=os.environ.get("NOTIFY_TOKEN"), help="auth token")
    p.add_argument("--json", action="store_true", help="print raw JSON responses")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_send_opts(sp):
        sp.add_argument("--duration-ms", type=int, dest="duration_ms", help="auto-close after N ms")
        sp.add_argument("--persistent", action="store_true", help="stay until cleared/clicked")
        sp.add_argument("--id", help="reuse to replace a toast in place")

    s = sub.add_parser("send", help="send a notification to a client (target '*' broadcasts)")
    s.add_argument("target", help="client id, or '*' to broadcast")
    s.add_argument("title")
    s.add_argument("message", nargs="?", default="")
    add_send_opts(s)
    s.add_argument("--queue-offline", action="store_true", dest="queue_offline",
                   help="if target offline, queue and deliver on reconnect (default: drop)")
    s.set_defaults(func=cmd_send)

    b = sub.add_parser("broadcast", help="send to every connected client")
    b.add_argument("title")
    b.add_argument("message", nargs="?", default="")
    add_send_opts(b)
    b.set_defaults(func=cmd_broadcast, queue_offline=False, target="*")

    c = sub.add_parser("clear", help="dismiss a toast on a client")
    c.add_argument("target", help="client id, or '*' for all clients")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--id", help="clear the toast with this id")
    g.add_argument("--all", action="store_true", help="clear every toast on the target")
    c.set_defaults(func=cmd_clear)

    sub.add_parser("clients", help="list connected clients and offline queues").set_defaults(func=cmd_clients)
    sub.add_parser("lock", help="show screen-lock status").set_defaults(func=cmd_lock)
    sub.add_parser("health", help="check the backend is up").set_defaults(func=cmd_health)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    url = resolve_url(args.url)
    return args.func(args, url, args.token)


if __name__ == "__main__":
    sys.exit(main())
