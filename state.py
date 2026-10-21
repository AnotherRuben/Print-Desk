"""Shared live state for the control panel."""
from __future__ import annotations

from collections import deque
from datetime import datetime


class State:
    def __init__(self):
        self.paused = False          # Twitch/tip slips are held while paused
        self.held = []               # slips waiting for resume
        self.last = None             # last slip sent, for reprint
        self.log = deque(maxlen=150)
        self.conn = {}               # name -> {"level": ok|warn|error|off, "detail": str}

    def status(self, name, level, detail=""):
        self.conn[name] = {"level": level, "detail": detail,
                           "since": datetime.now().strftime("%H:%M:%S")}

    def add_log(self, kind, text):
        self.log.appendleft({"time": datetime.now().strftime("%H:%M"), "kind": kind, "text": text})
