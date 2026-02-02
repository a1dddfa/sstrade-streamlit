# -*- coding: utf-8 -*-
"""
Short Trailing bot implementation
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional

from bots.base import BotBase
from bots.common.ticker_mixin import TickerSubscriptionMixin
from .logic import ShortTrailingLogic
from infra.logging.ui_logger import UILogger

# Import exchange type for hints / usage
try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange  # type: ignore



class ShortTrailingConfig:
    # Trading basics
    symbol: str = "ETHUSDT"
    qty: float = 0.01

    # Strategy distances (in price units)
    stop_limit_distance: float = 30.0
    stop_market_extra_distance: float = 20.0
    next_entry_distance: float = 40.0
    cancel_distance: float = 60.0
    reentry_distance: float = 30.0

    # --- Entry (maker-only) controls ---
    # When enabled, entry uses a post-only limit order pegged to the order book
    # (short: best ask; long: best bid) and will auto reprice by cancel+replace.
    entry_maker_only: bool = True

    # Entry reprice throttles
    # Only reprice when best-ask changes by at least this amount.
    entry_min_price_delta: float = 0.5
    # Minimum seconds between entry cancel+replace.
    entry_min_replace_interval_sec: float = 0.3

    # Give up chasing after this many seconds (0 = never give up).
    entry_max_chase_sec: float = 10.0

    # Tag prefix for order identification
    tag_prefix: str = "UI_SHORTTRAIL"

    # --- Throttle controls (user-adjustable) ---
    # Minimum change in stop-limit price to trigger an update.
    # Example: 1.0 means only update after at least 1.0 price move in stop.
    min_stop_price_delta: float = 1.0

    # Minimum seconds between stop updates (cancel+recreate).
    # Example: 0.5 means at most 2 updates per second.
    min_replace_interval_sec: float = 0.5


class ShortTrailingBot(BotBase, TickerSubscriptionMixin):
    def __init__(self, exchange, config):
        super().__init__()
        BotBase.__init__(self, name="ShortTrailingBot")
        self.exchange = exchange
        self.cfg = config
        self.log = UILogger()

        self.logic = ShortTrailingLogic(exchange, config)

        # WS rebuild can cause missed FILLED events. Use a flag to trigger
        # REST reconciliation on the next tick (avoid doing REST inside WS thread).
        self._need_resync_from_exchange = False

    def on_ticker(self, price):
        # ✅ After user-stream rebuild, do a best-effort reconciliation.
        if getattr(self, "_need_resync_from_exchange", False):
            try:
                self._need_resync_from_exchange = False
                self.logic.resync_from_exchange()
            except Exception:
                pass
        if self.logic.state.position_open:
            self.logic.on_price(price)
        else:
            self.logic.on_price_no_position(price)

    def start(self) -> None:
        """Start ticker subscription."""
        def _factory():
            def _cb(t):
                px = t.get("price") or t.get("last") or t.get("lastPrice") or t.get("c")
                if px is None:
                    return
                try:
                    self.on_ticker(float(px))
                except Exception:
                    return
            return _cb

        self._ensure_ticker_ws(self.exchange, self.cfg.symbol, _factory, on_log=self.log.log)

    def stop(self) -> None:
        try:
            self._unsubscribe_ticker_if_any(self.exchange)
        except Exception:
            pass

    # =========================
    # ✅ WS 断线/重建后：触发下一轮 tick 做 REST 对账
    # =========================
    def on_ws_event(self, e: Dict[str, Any]) -> None:
        """Called from websocket background threads via dispatcher.

        NOTE: DO NOT call Streamlit APIs or touch st.session_state here.
        """
        try:
            if (e.get("event") or "") != "user_stream_rebuild":
                return
            if (e.get("stage") or "") != "ok":
                return
            self._need_resync_from_exchange = True
            # If the underlying ws_manager restarted, ticker subscription state may be stale.
            # Clearing these lets TickerSubscriptionMixin re-subscribe on next tick if needed.
            try:
                self._ticker_symbol = None  # type: ignore[attr-defined]
                self._ticker_cb = None      # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass

    def on_user_stream_order_update(self, order):
        try:
            if (order.get("status") or "").upper() != "FILLED":
                return

            cid = str(order.get("clientOrderId") or "")
            avg = float(order.get("avgPrice") or 0.0)
            fill_price = avg if avg > 0 else float(order.get("price") or 0.0)

            if f"{self.cfg.tag_prefix}_ENTRY" in cid:
                self.logic.on_entry_filled(fill_price)
                return

            if (
                f"{self.cfg.tag_prefix}_STOP_LIMIT" in cid
                or f"{self.cfg.tag_prefix}_STOP_MARKET" in cid
            ):
                self.logic.on_exit_filled(fill_price)
                return
        except Exception:
            return
