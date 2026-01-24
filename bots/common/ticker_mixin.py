# -*- coding: utf-8 -*-
"""
Ticker subscription mixin for bots.

Extracted from app/streamlit_app.py (commit #4 split).
"""
from __future__ import annotations
from typing import Callable, Optional, Any


class TickerSubscriptionMixin:
    """Provide subscribe/unsubscribe helper for exchange ticker websocket."""

    def __init__(self):
        self._ticker_symbol: Optional[str] = None
        self._ticker_cb: Optional[Callable[[Any], None]] = None

    def _subscribe_ticker_if_any(self, exchange: Any, symbol: str, cb: Callable[[Any], None]) -> None:
        self._ticker_symbol = symbol
        self._ticker_cb = cb
        try:
            exchange.ws_subscribe_ticker(symbol, cb)
        except Exception:
            # subscribe failures shouldn't crash bot init
            pass

    def _unsubscribe_ticker_if_any(self, exchange: Any) -> None:
        if not self._ticker_symbol:
            return
        try:
            exchange.ws_unsubscribe_ticker(self._ticker_symbol, self._ticker_cb)
        except Exception:
            try:
                exchange.ws_unsubscribe_ticker(self._ticker_symbol)
            except Exception:
                pass
        finally:
            self._ticker_symbol = None
            self._ticker_cb = None
