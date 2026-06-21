#!/usr/bin/env python3
"""Notification client agent for macOS.

Connects to the central backend (see ``server.py``) over a WebSocket, identifies
itself with a client id, and renders received notifications as desktop toasts in
the top-right corner of the screen. Reconnects automatically.

  NOTIFY_BACKEND  ws://<backend-host>:8766   (default ws://localhost:8766)
  NOTIFY_CLIENT   this client's id           (default: hostname)
  NOTIFY_TOKEN    auth token, if the backend requires one

To push to a client, POST to the backend's /notify with {"target": "<id>", ...}.
"""

import asyncio
import json
import os
import queue
import socket
import threading
import tkinter as tk
from typing import Optional
from urllib.parse import quote

import websockets


BACKEND = os.environ.get("NOTIFY_BACKEND", "ws://localhost:8766").rstrip("/")
CLIENT_ID = os.environ.get("NOTIFY_CLIENT") or socket.gethostname()
AUTH_TOKEN = os.environ.get("NOTIFY_TOKEN") or None
RECONNECT_DELAY_S = 2.0

TOAST_DURATION_MS = 6000
TOAST_WIDTH = 360
TOAST_PADDING = 14
TOAST_MARGIN = 18
TOAST_GAP = 8

BG = "#1e1e2e"
BORDER = "#45475a"
TITLE_FG = "#f5e0dc"
MSG_FG = "#cdd6f4"


notification_queue: "queue.Queue[dict]" = queue.Queue()
clear_queue: "queue.Queue[dict]" = queue.Queue()


class ToastManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.active: list[tk.Toplevel] = []
        self.by_id: dict[str, tk.Toplevel] = {}

    def show(self, title: str, message: str, duration_ms: int,
             toast_id: Optional[str] = None, persistent: bool = False):
        # Reusing an id replaces the existing toast (update in place).
        if toast_id is not None and toast_id in self.by_id:
            self._destroy(self.by_id[toast_id])
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        # macOS: render as a non-activating floating panel so the toast layers
        # on top WITHOUT stealing focus or making this app the active one (same
        # window class IDLE uses for tooltips). Without this, creating the window
        # activates the Python app and takes over the foreground UI.
        try:
            if win.tk.call("tk", "windowingsystem") == "aqua":
                win.tk.call("::tk::unsupported::MacWindowStyle",
                            "style", win._w, "help", "noActivates")
        except tk.TclError:
            pass
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.96)
        except tk.TclError:
            pass

        frame = tk.Frame(
            win, bg=BG, padx=TOAST_PADDING, pady=TOAST_PADDING,
            highlightbackground=BORDER, highlightthickness=1,
        )
        frame.pack(fill="both", expand=True)

        wrap_px = TOAST_WIDTH - 2 * TOAST_PADDING

        title_lbl = tk.Label(
            frame, text=title, bg=BG, fg=TITLE_FG,
            font=("Helvetica", 13, "bold"),
            wraplength=wrap_px, justify="left", anchor="w",
        )
        title_lbl.pack(fill="x")

        widgets = [win, frame, title_lbl]

        if message:
            msg_lbl = tk.Label(
                frame, text=message, bg=BG, fg=MSG_FG,
                font=("Helvetica", 11),
                wraplength=wrap_px, justify="left", anchor="w",
            )
            msg_lbl.pack(fill="x", pady=(4, 0))
            widgets.append(msg_lbl)

        self.active.append(win)
        if toast_id is not None:
            self.by_id[toast_id] = win
        self._reposition()

        for w in widgets:
            w.bind("<Button-1>", lambda _evt, wn=win: self._destroy(wn))

        if not persistent:
            win.after(duration_ms, lambda wn=win: self._destroy(wn))

    def _destroy(self, win: tk.Toplevel):
        if win in self.active:
            self.active.remove(win)
        for tid, w in list(self.by_id.items()):
            if w is win:
                del self.by_id[tid]
        try:
            win.destroy()
        except tk.TclError:
            pass
        self._reposition()

    def clear(self, toast_id: str):
        win = self.by_id.get(toast_id)
        if win is not None:
            self._destroy(win)

    def clear_all(self):
        for win in list(self.active):
            self._destroy(win)

    def _reposition(self):
        screen_w = self.root.winfo_screenwidth()
        y = TOAST_MARGIN
        for win in list(self.active):
            try:
                win.update_idletasks()
                h = win.winfo_reqheight()
                x = screen_w - TOAST_WIDTH - TOAST_MARGIN
                win.geometry(f"{TOAST_WIDTH}x{h}+{x}+{y}")
                y += h + TOAST_GAP
            except tk.TclError:
                pass


def _ws_url() -> str:
    url = f"{BACKEND}/ws?client={quote(CLIENT_ID, safe='')}"
    if AUTH_TOKEN:
        url += f"&token={quote(AUTH_TOKEN, safe='')}"
    return url


async def ws_loop():
    """Connect to the backend and feed received messages into the Tk queues."""
    url = _ws_url()
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                print(f"connected to {BACKEND} as '{CLIENT_ID}'")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if msg.get("type") == "notify":
                        notification_queue.put(msg)
                    elif msg.get("type") == "clear":
                        clear_queue.put(msg)
        except Exception as exc:
            print(f"disconnected ({exc.__class__.__name__}: {exc}); "
                  f"retrying in {RECONNECT_DELAY_S:.0f}s")
        await asyncio.sleep(RECONNECT_DELAY_S)


def main():
    print(f"Notification client '{CLIENT_ID}' -> {BACKEND}")
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    manager = ToastManager(root)

    def poll():
        try:
            while True:
                n = notification_queue.get_nowait()
                duration = n.get("duration_ms") or TOAST_DURATION_MS
                manager.show(
                    n["title"], n.get("message", ""), duration,
                    toast_id=n.get("id"), persistent=bool(n.get("persistent")),
                )
        except queue.Empty:
            pass
        try:
            while True:
                cmd = clear_queue.get_nowait()
                if cmd.get("all"):
                    manager.clear_all()
                elif cmd.get("id"):
                    manager.clear(cmd["id"])
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
