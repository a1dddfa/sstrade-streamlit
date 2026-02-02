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
        # 多 consumer 支持
        self._range2_bot: Optional[OrderUpdateConsumer] = None
        self._order_consumers: list[Any] = []
        # WS 事件消费者（例如：user_stream 重建成功后，策略做一次 REST 对账/重建 state）
        self._ws_event_consumers: list[Any] = []

    def register_ui_logger(self, ui_logger: Optional[Any]) -> None:
        with self._lock:
            self._ui_logger = ui_logger

    def register_range2_bot(self, bot: Optional[OrderUpdateConsumer]) -> None:
        with self._lock:
            self._range2_bot = bot
            self._order_consumers = [b for b in self._order_consumers if b is not bot]
            if bot is not None:
                self._order_consumers.append(bot)

    def register_order_consumer(self, bot):
        with self._lock:
            self._order_consumers = [b for b in self._order_consumers if b is not bot]
            if bot is not None:
                self._order_consumers.append(bot)

    def register_ws_event_consumer(self, consumer: Optional[Any]) -> None:
        """Register a consumer that wants WS connection lifecycle events.

        Consumer may implement:
            on_ws_event(event: Dict[str, Any]) -> None
        """
        with self._lock:
            self._ws_event_consumers = [b for b in self._ws_event_consumers if b is not consumer]
            if consumer is not None:
                self._ws_event_consumers.append(consumer)

    def handle_ws_event(self, e: Dict[str, Any]) -> None:
        """Exchange WS event callback (runs in background threads).

        This should never raise.
        """
        # 1) UI log
        try:
            with self._lock:
                ui_logger = self._ui_logger
            if ui_logger is not None:
                ui_logger.log(
                    f"[WS] {e.get('event')} stage={e.get('stage')} reason={e.get('reason')}"
                )
        except Exception:
            pass

        # 2) forward to consumers
        try:
            with self._lock:
                consumers = list(self._ws_event_consumers)
            for c in consumers:
                fn = getattr(c, "on_ws_event", None)
                if callable(fn):
                    try:
                        fn(e)
                    except Exception:
                        # swallow to protect WS threads
                        pass
        except Exception:
            pass

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
                consumers = list(self._order_consumers)
            for c in consumers:
                try:
                    c.on_user_stream_order_update(o)
                except Exception:
                    pass
        except Exception:
            pass
