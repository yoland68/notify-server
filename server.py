#!/usr/bin/env python3
"""Tailscale-reachable toast notification server for macOS.

POST /notify  {"title": "...", "message": "...", "duration_ms": 6000}
GET  /health
"""

import os
import queue
import socket
import threading
import tkinter as tk
from typing import Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


HOST = os.environ.get("NOTIFY_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOTIFY_PORT", "8765"))
AUTH_TOKEN = os.environ.get("NOTIFY_TOKEN") or None

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


class Notification(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    message: str = Field("", max_length=2000)
    duration_ms: Optional[int] = Field(None, ge=500, le=60000)


app = FastAPI()


@app.post("/notify")
def notify(n: Notification, x_auth_token: Optional[str] = Header(default=None)):
    if AUTH_TOKEN and x_auth_token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")
    notification_queue.put(n.model_dump())
    return {"status": "queued"}


@app.get("/health")
def health():
    return {"status": "ok"}


class ToastManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.active: list[tk.Toplevel] = []

    def show(self, title: str, message: str, duration_ms: int):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
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
        self._reposition()

        def close(_evt=None):
            if win in self.active:
                self.active.remove(win)
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._reposition()

        for w in widgets:
            w.bind("<Button-1>", close)

        win.after(duration_ms, close)

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


def run_server():
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server.run()


def main():
    print(f"Notification server listening on http://{HOST}:{PORT}")
    for ip in _local_ips():
        print(f"  reachable at http://{ip}:{PORT}/notify")
    if AUTH_TOKEN:
        print("  auth: X-Auth-Token header required")

    threading.Thread(target=run_server, daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    manager = ToastManager(root)

    def poll():
        try:
            while True:
                n = notification_queue.get_nowait()
                duration = n.get("duration_ms") or TOAST_DURATION_MS
                manager.show(n["title"], n.get("message", ""), duration)
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
