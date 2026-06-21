#!/usr/bin/env python3
"""Integration test: a toast must not take over the active UI (macOS).

Brings TextEdit to the foreground and types into it continuously; midway it
fires a notification at a real client.py. If the toast *activates* (takes over
the foreground), the frontmost app stays the toast process ("python") for the
rest of the typing — that's the failure. The fix lets the toast appear but keeps
the foreground on TextEdit, so frontmost returns to it immediately.

Run directly:   python test_focus.py
Or via pytest:  pytest test_focus.py

Requires a logged-in macOS GUI session and Accessibility permission for the
process running it (so System Events can type). It opens/closes its own TextEdit
document and runs a private backend + client on an ephemeral port.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
VICTIM = "TextEdit"
CLIENT_ID = "focustest"
MARK = "x"
KEYSTROKES = 25
TRIGGER_AT = 8          # fire the toast after this many keystrokes (~1s in)
KEY_GAP_S = 0.12
TAKEOVER_FRACTION = 0.25  # > this share of post-toast samples non-victim => takeover


def _osa(*lines):
    args = ["osascript"]
    for ln in lines:
        args += ["-e", ln]
    return subprocess.run(args, capture_output=True, text=True)


def frontmost():
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                         capture_output=True, text=True).stdout
    return out.split("=")[-1].strip().strip('"')


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or "null")


def _wait_connected(base, cid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cid in _http("GET", base + "/clients").get("connected", {}):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run():
    if sys.platform != "darwin":
        print("SKIP: macOS only")
        return True

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    backend = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "server.py")],
        env={**os.environ, "NOTIFY_PORT": str(port), "NOTIFY_ON_LOCK": "0"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "client.py")],
        env={**os.environ, "NOTIFY_CLIENT": CLIENT_ID,
             "NOTIFY_BACKEND": f"ws://127.0.0.1:{port}"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        if not _wait_connected(base, CLIENT_ID):
            raise AssertionError("setup: test client never connected to the backend")

        # Victim: a fresh, focused TextEdit document.
        _osa('tell application "TextEdit" to activate',
             'tell application "TextEdit" to make new document',
             'delay 0.4',
             'tell application "TextEdit" to set text of document 1 to ""')
        time.sleep(0.4)
        if frontmost() != VICTIM:
            _osa('tell application "TextEdit" to activate', 'delay 0.5')
        if frontmost() != VICTIM:
            raise AssertionError(f"setup: could not focus {VICTIM} (frontmost={frontmost()!r})")

        post_samples = []      # frontmost after each keystroke, once the toast is up
        delivered = False
        for i in range(KEYSTROKES):
            _osa(f'tell application "System Events" to keystroke "{MARK}"')
            if i == TRIGGER_AT:
                resp = _http("POST", base + "/notify", {
                    "target": CLIENT_ID, "title": "Focus test",
                    "message": "typing should keep going in TextEdit",
                    "persistent": True, "id": "focus-it",
                })
                delivered = bool(resp.get("delivered"))
            time.sleep(KEY_GAP_S)
            if i >= TRIGGER_AT:
                post_samples.append(frontmost())

        typed = _osa('tell application "TextEdit" to get text of document 1').stdout.count(MARK)

        # --- assertions ---
        if not delivered:
            raise AssertionError("toast was not delivered to the test client")

        non_victim = [s for s in post_samples if s != VICTIM]
        frac = len(non_victim) / len(post_samples) if post_samples else 0.0
        if frac > TAKEOVER_FRACTION:
            stealer = max(set(non_victim), key=non_victim.count)
            raise AssertionError(
                f"toast took over the UI: frontmost was {stealer!r} for "
                f"{frac*100:.0f}% of typing after the toast "
                f"(samples={post_samples})")
        if frontmost() != VICTIM:
            raise AssertionError(
                f"foreground not restored: frontmost ended {frontmost()!r}, expected {VICTIM}")

        print(f"PASS: toast did not take over the UI "
              f"(non-victim frontmost {frac*100:.0f}% of post-toast samples; "
              f"{typed}/{KEYSTROKES} keystrokes landed in {VICTIM})")
        return True
    finally:
        _osa('tell application "TextEdit" to close every document saving no')
        for p in (client, backend):
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def test_toast_does_not_take_over_ui():
    assert run()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
