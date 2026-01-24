# -*- coding: utf-8 -*-
"""
User stream dispatcher: receive Binance user-stream order updates in WS callback thread,
then forward to registered bot(s) + ui logger.

Extracted from streamlit_app.py (commit #1 split).

Notes:
- WS callback runs in background thread; do NOT touch st.session_state here.
- This dispatcher is thread-safe via an internal RLock.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Protocol


class OrderUpdateConsumer(Protocol):
    """Anything that can consume an order update from user stream."""
    def on_user_stream_order_update(self, order: Dict[str, Any]) -> None:  # pragma: no cover
        ...


class UserStreamDispatcher:
    def __init__(self):
        self._lock = threading.RLock()
        self._ui_logger: Optional[Any] = None  # expects .log(str)
        self._range2_bot: Optional[OrderUpdateConsumer] = None

    def register_ui_logger(self, ui_logger: Optional[Any]) -> None:
        with self._lock:
            self._ui_logger = ui_logger

    def register_range2_bot(self, bot: Optional[OrderUpdateConsumer]) -> None:
        with self._lock:
            self._range2_bot = bot

    def handle_order_update(self, o: Dict[str, Any]) -> None:
        """
        A pure-Python websocket callback to be passed into BinanceExchange.ws_subscribe_user_stream.
        It will:
        1) write a short line into UI logger (if registered)
        2) forward update to RangeTwoBot (if registered)
        Never raises (exceptions are swallowed to protect WS loop).
        """
        # 1) UI log
        try:
            with self._lock:
                ui_logger = self._ui_logger
            if ui_logger is not None:
                ui_logger.log(
                    f"[ORDER] {o.get('symbol')} {o.get('status')} "
                    f"side={o.get('side')} posSide={o.get('positionSide')} "
                    f"tag={o.get('tag')} avg={o.get('avgPrice')} clientId={o.get('clientOrderId')}"
                )
        except Exception:
            pass

        # 2) dispatch to bot
        try:
            with self._lock:
                rb = self._range2_bot
            if rb is not None:
                rb.on_user_stream_order_update(o)
        except Exception:
            pass
