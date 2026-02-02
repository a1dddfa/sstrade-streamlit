# -*- coding: utf-8 -*-
"""
Bot base class: unify thread lifecycle (start/stop) + shared primitives.

This is extracted as "commit #2 split". It is intentionally minimal and non-invasive:
- Existing bots may still keep their own locks/fields.
- Bots can call `_start_thread(self._run)` and `_stop_thread()` to reduce duplication.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class BotBase:
    def __init__(self, name: str = "Bot"):
        self._bot_name = str(name)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _start_thread(self, target: Callable[[], None]) -> None:
        """Start daemon thread once. Clears stop flag."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=target, name=self._bot_name, daemon=True)
        self._thread.start()

    def _stop_thread(self) -> None:
        """Signal stop. (Thread is daemon; no join here.)"""
        self._stop.set()

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    def on_ticker(self, price):
        pass
