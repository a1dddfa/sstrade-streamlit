# -*- coding: utf-8 -*-
import time
import logging

log = logging.getLogger("short_trailing_stack")
trade_log = logging.getLogger("trade")


def _safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


class ShortTrailingStackState:
    # Position state
    position_open = False

    # Entry trackers (same as original)
    entry_order_id = None
    entry_price = None
    entry_client_id = None
    entry_algo_id = None
    entry_fill_price = None

    # Stop stack trackers
    stop_limit_order_ids = None  # list[str]
    stop_limit_algo_ids = None   # list[str]
    last_price_seen = None

    exit_price = None


class ShortTrailingStackLogic:
    """
    Variant of ShortTrailing:
      - Entry logic unchanged (STOP_LIMIT short at bid2).
      - Exit protection: ONLY STOP_LIMIT trigger-limit orders (no stop-market, no force-close).
      - On EVERY price tick (up or down), place a NEW stop-limit order at (market_price + stop_limit_distance).
      - Do NOT cancel previous stop orders when placing a new one (stacking).
      - Only when exit filled: cancel all orders, reset state, and place next entry.
    """

    def __init__(self, exchange, config):
        self.exchange = exchange
        self.cfg = config
        self.state = ShortTrailingStackState()
        self.state.stop_limit_order_ids = []
        self.state.stop_limit_algo_ids = []

        # Entry chasing throttle knobs reused from original short_trailing
        self._last_entry_price_sent = None
        self._last_entry_replace_ts = 0.0
        self._entry_chase_start_ts = 0.0

    # ---------- book helpers ----------
    def _get_book(self):
        book = self.exchange.get_order_book(self.cfg.symbol)
        if not isinstance(book, dict):
            raise RuntimeError("order_book_invalid")
        return book

    def _get_bid2(self) -> float:
        book = self._get_book()
        bids = book.get("bids") or []
        if len(bids) < 2:
            # fallback to best bid
            if len(bids) >= 1:
                return float(bids[0][0])
            raise RuntimeError("no_bids")
        return float(bids[1][0])

    def _get_best_ask(self) -> float:
        book = self._get_book()
        asks = book.get("asks") or []
        if not asks:
            raise RuntimeError("no_asks")
        return float(asks[0][0])

    # ---------- entry order refs ----------
    def _extract_entry_refs(self, order: dict):
        oid = order.get("orderId") or order.get("id")
        aid = order.get("algoId") or order.get("algoOrderId")
        cid = order.get("clientOrderId") or order.get("clientAlgoId") or order.get("tag")
        return oid, aid, cid

    def _cancel_entry(self) -> bool:
        s = self.state
        if not s.entry_order_id and not s.entry_algo_id:
            return True
        try:
            self.exchange.cancel_order_any(
                symbol=self.cfg.symbol,
                order_id=s.entry_order_id,
                algo_id=s.entry_algo_id,
            )
            return True
        except Exception:
            return False

    def _sync_entry_open_status(self) -> None:
        """Best-effort: check entry order status; clear local tracker if not open anymore."""
        s = self.state
        if not s.entry_order_id and not s.entry_algo_id:
            return
        try:
            od = self.exchange.get_order_any(
                symbol=self.cfg.symbol,
                order_id=s.entry_order_id,
                algo_id=s.entry_algo_id,
            )
        except Exception:
            return
        status = str((od or {}).get("status") or "").upper()
        if status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
            s.entry_order_id = None
            s.entry_algo_id = None
            s.entry_client_id = None
            s.entry_price = None

    def _sync_position_fallback(self) -> None:
        """If we missed FILLED, rebuild state from positions."""
        s = self.state
        if s.position_open:
            return
        try:
            positions = self.exchange.get_positions(self.cfg.symbol) or []
        except Exception:
            return
        for p in positions:
            ps = str(p.get("positionSide") or "").upper()
            amt = _safe_float(p.get("positionAmt"), default=0.0) or 0.0
            is_short = (ps == "SHORT") or (ps == "BOTH" and amt < 0)
            if not is_short:
                continue
            if abs(float(amt)) <= 0:
                continue
            ep = (
                _safe_float(p.get("breakEvenPrice"), default=None)
                or _safe_float(p.get("entryPrice"), default=None)
                or 0.0
            )
            if ep and float(ep) > 0:
                self.on_entry_filled(float(ep))
            return

    def resync_from_exchange(self) -> None:
        self._sync_position_fallback()
        self._sync_entry_open_status()

    # ---------- entry ----------
    def place_entry(self, price, *, maker_only: bool = False) -> bool:
        # Keep the original style: STOP_LIMIT short, stopPrice==limit price.
        tif = "GTC"
        log.info(
            "ENTRY_PLACE attempt | symbol=%s price=%.8f qty=%s stop=%.8f tag=%s",
            self.cfg.symbol, float(price), str(self.cfg.qty), float(price), f"{self.cfg.tag_prefix}_ENTRY",
        )
        try:
            order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                side="short",
                order_type="stop_limit",
                quantity=self.cfg.qty,
                price=price,
                params={
                    "timeInForce": tif,
                    "stopPrice": price,
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_ENTRY",
                },
            )
        except Exception:
            log.exception("ENTRY_PLACE failed | symbol=%s price=%.8f qty=%s", self.cfg.symbol, float(price), str(self.cfg.qty))
            self.state.entry_order_id = None
            self.state.entry_price = None
            self.state.entry_client_id = None
            self.state.entry_algo_id = None
            return False

        oid, aid, cid = self._extract_entry_refs(order or {})
        self.state.entry_order_id = oid
        self.state.entry_algo_id = aid
        self.state.entry_client_id = cid
        self.state.entry_price = price

        trade_log.info("SHORTTRAILSTACK_ENTRY_PLACED | symbol=%s order_id=%s price=%.8f", self.cfg.symbol, str(oid), float(price))
        self._last_entry_price_sent = float(price)
        self._last_entry_replace_ts = time.time()
        if self._entry_chase_start_ts == 0.0:
            self._entry_chase_start_ts = time.time()
        return True

    def place_entry_trigger_bid2(self) -> None:
        bid2 = self._get_bid2()
        self.place_entry(bid2, maker_only=False)

    def place_entry_maker_best_ask(self) -> None:
        best_ask = self._get_best_ask()
        self.place_entry(best_ask, maker_only=True)

    # ---------- fill handlers ----------
    def on_entry_filled(self, fill_price: float) -> None:
        s = self.state
        log.info("ENTRY_FILLED | symbol=%s fill=%.8f", self.cfg.symbol, float(fill_price))
        trade_log.info("SHORTTRAILSTACK_ENTRY_FILLED | symbol=%s fill=%.8f", self.cfg.symbol, float(fill_price))

        s.position_open = True
        s.entry_fill_price = float(fill_price)
        s.last_price_seen = None  # reset tick guard

        # First stop placement can wait until the next on_price tick; but we also place immediately.
        self._place_stop_limit_at_market(float(fill_price))

    def _place_stop_limit_at_market(self, market_price: float) -> None:
        """
        Place ONE stop-limit close order at (market_price + stop_limit_distance).
        IMPORTANT: do NOT use reduceOnly (per requirement).
        """
        s = self.state
        stop_px = float(market_price) + float(self.cfg.stop_limit_distance)

        try:
            o = self.exchange.create_order(
                symbol=self.cfg.symbol,
                side="long",  # close short
                order_type="stop_limit",
                quantity=self.cfg.qty,
                price=stop_px,
                params={
                    "stopPrice": stop_px,
                    "timeInForce": "GTC",
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_STOP_LIMIT",
                },
            )
        except Exception:
            # No force close; just log and rely on subsequent ticks / manual intervention.
            log.exception("STOP_LIMIT_PLACE failed | symbol=%s stop=%.8f", self.cfg.symbol, float(stop_px))
            return

        oid = str((o or {}).get("orderId") or "")
        aid = str((o or {}).get("algoId") or (o or {}).get("algoOrderId") or "")
        if oid:
            s.stop_limit_order_ids.append(oid)
        if aid:
            s.stop_limit_algo_ids.append(aid)

        trade_log.info("SHORTTRAILSTACK_STOP_PLACED | symbol=%s order_id=%s stop=%.8f", self.cfg.symbol, oid, float(stop_px))

    def on_price(self, price: float) -> None:
        s = self.state
        if not s.position_open:
            return

        # Every price change (up or down) => place a new stop order.
        # Guard against duplicate calls with same price value.
        if s.last_price_seen is not None and float(price) == float(s.last_price_seen):
            return
        s.last_price_seen = float(price)

        self._place_stop_limit_at_market(float(price))

    def on_exit_filled(self, price: float) -> None:
        s = self.state
        s.exit_price = float(price)
        trade_log.info("SHORTTRAILSTACK_EXIT_FILLED | symbol=%s fill=%.8f", self.cfg.symbol, float(price))

        # Only at exit: cancel everything (including stacked stops)
        try:
            self.exchange.cancel_all_orders(symbol=self.cfg.symbol)
        except Exception:
            pass

        # Reset cycle state
        s.position_open = False
        s.entry_order_id = None
        s.entry_price = None
        s.entry_client_id = None
        s.entry_algo_id = None
        s.entry_fill_price = None
        s.stop_limit_order_ids = []
        s.stop_limit_algo_ids = []
        s.last_price_seen = None

        # Reset entry chasing state
        self._last_entry_price_sent = None
        self._last_entry_replace_ts = 0.0
        self._entry_chase_start_ts = 0.0

        # Next cycle entry
        self.place_entry_trigger_bid2()

    # ---------- no position tick ----------
    def _should_replace_entry(self, *, new_entry_price: float) -> bool:
        min_delta = float(getattr(self.cfg, "entry_min_price_delta", 0.0) or 0.0)
        min_interval = float(getattr(self.cfg, "entry_min_replace_interval_sec", 0.5) or 0.0)

        if self._last_entry_price_sent is not None:
            if abs(float(new_entry_price) - float(self._last_entry_price_sent)) < min_delta:
                return False

        now = time.time()
        if (now - float(self._last_entry_replace_ts or 0.0)) < min_interval:
            return False

        max_chase = float(getattr(self.cfg, "entry_max_chase_sec", 0.0) or 0.0)
        if max_chase > 0:
            if self._entry_chase_start_ts == 0.0:
                self._entry_chase_start_ts = now
            if (now - self._entry_chase_start_ts) > max_chase:
                return False

        return True

    def on_price_no_position(self, price: float) -> None:
        s = self.state

        # Safety net: if we have a position but missed WS, rebuild.
        self._sync_position_fallback()
        if s.position_open:
            return

        self._sync_entry_open_status()
        try:
            bid2 = self._get_bid2()
        except Exception:
            log.exception("GET_BID2 failed | symbol=%s", self.cfg.symbol)
            return

        if not s.entry_order_id and not s.entry_algo_id:
            self.place_entry(bid2, maker_only=False)
            return

        if not self._should_replace_entry(new_entry_price=float(bid2)):
            return

        if s.entry_price is None or float(bid2) != float(s.entry_price):
            if not self._cancel_entry():
                s.entry_order_id = None
                s.entry_algo_id = None
                s.entry_client_id = None
                return
            self.place_entry(bid2, maker_only=False)
