# -*- coding: utf-8 -*-
"""
Short Trailing Stack bot implementation (variant)

Differences vs ShortTrailing:
- Exit protection uses ONLY STOP_LIMIT trigger-limit orders (no stop-market, no force-close).
- On every ticker price change (up or down), place a NEW STOP_LIMIT close order at (market + stop_limit_distance).
- Do not cancel old stop orders when stacking; only cancel all on exit.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from bots.base import BotBase
from bots.common.ticker_mixin import TickerSubscriptionMixin
from infra.logging.ui_logger import UILogger

from .logic import ShortTrailingStackLogic

try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange  # type: ignore


class ShortTrailingStackConfig:
    symbol: str = "ETHUSDT"
    qty: float = 0.01

    # Place each stacked stop at (market_price + stop_limit_distance)
    stop_limit_distance: float = 30.0

    # Entry maker-only and chase controls (kept for parity; entry order is still STOP_LIMIT at bid2)
    entry_maker_only: bool = False
    entry_min_price_delta: float = 0.5
    entry_min_replace_interval_sec: float = 0.3
    entry_max_chase_sec: float = 10.0

    tag_prefix: str = "UI_SHORTTRAILSTACK"


class ShortTrailingStackBot(BotBase, TickerSubscriptionMixin):
    def __init__(self, exchange: Any, config: ShortTrailingStackConfig):
        BotBase.__init__(self, name="ShortTrailingStackBot")
        TickerSubscriptionMixin.__init__(self)
        self.exchange = exchange
        self.cfg = config
        self.log = UILogger()

        self.logic = ShortTrailingStackLogic(exchange, config)

        # WS rebuild can cause missed FILLED events
        self._need_resync_from_exchange = False

    def start(self) -> None:
        def _factory():
            def _cb(t: Dict[str, Any]):
                px = t.get("price") or t.get("last") or t.get("lastPrice") or t.get("c")
                if px is None:
                    return
                try:
                    self.on_ticker(float(px))
                except Exception:
                    return
            return _cb

        self._ensure_ticker_ws(self.exchange, self.cfg.symbol, _factory, on_log=self.log.log)

        # Kick off first entry if nothing exists yet.
        try:
            if not self.logic.state.position_open and not self.logic.state.entry_order_id and not self.logic.state.entry_algo_id:
                self.logic.place_entry_trigger_bid2()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self._unsubscribe_ticker_if_any(self.exchange)
        except Exception:
            pass

    def on_ws_event(self, e: Dict[str, Any]) -> None:
        try:
            if (e.get("event") or "") != "user_stream_rebuild":
                return
            if (e.get("stage") or "") != "ok":
                return
            self._need_resync_from_exchange = True
            # allow ticker mixin to resubscribe if needed
            self._ticker_symbol = None
            self._ticker_cb = None
        except Exception:
            pass

    def on_ticker(self, price: float) -> None:
        if self._need_resync_from_exchange:
            try:
                self._need_resync_from_exchange = False
                self.logic.resync_from_exchange()
            except Exception:
                pass

        if self.logic.state.position_open:
            self.logic.on_price(price)
        else:
            self.logic.on_price_no_position(price)

    # Called by UserStreamDispatcher (background thread)
    def on_user_stream_order_update(self, order: Dict[str, Any]) -> None:
        try:
            if (order.get("status") or "").upper() != "FILLED":
                return

            cid = str(order.get("clientOrderId") or "")
            tag = str(order.get("tag") or order.get("clientAlgoId") or order.get("origClientOrderId") or "")
            avg = float(order.get("avgPrice") or 0.0)
            fill_price = avg if avg > 0 else float(order.get("price") or 0.0)

            # Entry fill recognition
            if not self.logic.state.position_open:
                if f"{self.cfg.tag_prefix}_ENTRY" in cid or f"{self.cfg.tag_prefix}_ENTRY" in tag:
                    self.logic.on_entry_filled(fill_price)
                    return

            # Exit fill recognition: any STOP_LIMIT with our tag prefix
            if self.logic.state.position_open:
                if f"{self.cfg.tag_prefix}_STOP_LIMIT" in cid or f"{self.cfg.tag_prefix}_STOP_LIMIT" in tag:
                    self.logic.on_exit_filled(fill_price)
                    return
        except Exception:
            return
