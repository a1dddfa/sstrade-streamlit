# -*- coding: utf-8 -*-
"""
Thread-safe UI logger for Streamlit + background threads.

This is extracted from streamlit_app.py (commit #1 split).
"""
from __future__ import annotations

import threading
import time
from typing import List


class UILogger:
    def __init__(self, max_lines: int = 800):
        self.max_lines = int(max_lines)
        self._lock = threading.Lock()
        self._lines: List[str] = []

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self.max_lines:
                self._lines = self._lines[-self.max_lines :]

    def tail(self, n: int = 300) -> str:
        with self._lock:
            lines = self._lines[-int(n) :]
        return "\n".join(lines)
