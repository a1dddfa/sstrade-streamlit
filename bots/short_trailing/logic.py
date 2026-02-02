import time


def _safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

class ShortTrailingState:
    position_open = False

    entry_order_id = None
    entry_price = None

    entry_fill_price = None
    lowest_price = None   # ★ 核心：自己维护的最低价

    stop_limit_order_id = None
    stop_market_order_id = None

    exit_price = None


class ShortTrailingLogic:
    def __init__(self, exchange, config):
        self.exchange = exchange
        self.cfg = config
        self.state = ShortTrailingState()

        # --- throttle controls (configurable) ---
        # Only send stop updates if BOTH conditions pass:
        # 1) price delta >= cfg.min_stop_price_delta
        # 2) time delta  >= cfg.min_replace_interval_sec
        self._last_stop_limit_sent = None  # type: float | None
        self._last_replace_ts = 0.0

        # --- entry (maker-only) chase controls ---
        self._last_entry_price_sent = None  # type: float | None
        self._last_entry_replace_ts = 0.0
        self._entry_chase_start_ts = 0.0

    def _get_best_ask(self) -> float:
        """Return best ask from order book (float)."""
        ob = self.exchange.get_order_book(self.cfg.symbol, limit=5)
        asks = (ob or {}).get("asks") or []
        if not asks:
            raise RuntimeError("order book empty (asks)")
        # binance returns [[price, qty], ...] as strings
        px = _safe_float(asks[0][0])
        if px is None:
            raise RuntimeError("invalid best ask")
        return float(px)

    def _should_replace_entry(self, *, new_entry_price: float) -> bool:
        """Throttle entry cancel+replace when maker-only chasing."""
        min_price_delta = float(getattr(self.cfg, "entry_min_price_delta", 0.0) or 0.0)
        min_interval = float(getattr(self.cfg, "entry_min_replace_interval_sec", 0.2) or 0.0)

        if self._last_entry_price_sent is not None:
            if abs(new_entry_price - self._last_entry_price_sent) < min_price_delta:
                return False

        now = time.time()
        if (now - self._last_entry_replace_ts) < min_interval:
            return False

        max_chase = float(getattr(self.cfg, "entry_max_chase_sec", 0.0) or 0.0)
        if max_chase > 0 and self._entry_chase_start_ts > 0:
            if (now - self._entry_chase_start_ts) > max_chase:
                return False

        return True

    def _sync_entry_open_status(self) -> None:
        """Sync local entry order state with exchange OPEN orders.

        GTX (post-only) rejections do not create an OPEN order. Also, orders can
        be canceled/filled externally. We therefore treat the exchange open
        orders list as the source of truth when no position.
        """
        s = self.state
        # If we already believe a position is open, don't mutate entry state here.
        if s.position_open:
            return
        if not s.entry_order_id:
            return

        try:
            open_orders = self.exchange.get_open_orders(symbol=self.cfg.symbol)
        except Exception:
            # If open_orders is unavailable (rate-limit/degraded), don't mutate state.
            return

        open_ids = {str(o.get("orderId")) for o in (open_orders or []) if o.get("orderId") is not None}
        if str(s.entry_order_id) in open_ids:
            return

        # ✅ Entry order disappeared from OPEN orders.
        # It can be FILLED / CANCELED / EXPIRED / REJECTED.
        # WS can miss the FILLED event, so do a REST reconciliation here.
        try:
            od = self.exchange.get_order(str(s.entry_order_id), self.cfg.symbol)
            st = str((od or {}).get("status") or "").upper()
            avg = _safe_float((od or {}).get("avgPrice"), default=0.0) or 0.0
            px = avg if avg > 0 else (_safe_float((od or {}).get("price"), default=0.0) or 0.0)
            if st == "FILLED" and px > 0:
                self.on_entry_filled(float(px))
                return
        except Exception:
            # If get_order fails (rate-limit/transient), don't clear state yet.
            return

        # Not filled -> clear local entry trackers
        s.entry_order_id = None
        s.entry_price = None

    def _sync_position_fallback(self) -> None:
        """Best-effort fallback: if a SHORT position exists but we missed FILLED, rebuild state.

        This is a stronger reconciliation than order-status: even if order history is unavailable
        or avgPrice is missing, positions should reflect the truth.
        """
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

            # Hedge mode: positionSide == SHORT is ideal.
            # One-way mode: positionSide == BOTH and amt < 0 means net short.
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
        """Public hook: reconcile state after WS rebuild or suspected missed events."""
        # Prefer position-based truth first, then order-status.
        self._sync_position_fallback()
        self._sync_entry_open_status()

    # ① 挂初始限价空单
    def place_entry(self, price, *, maker_only: bool = False) -> bool:
        tif = "GTX" if maker_only else "GTC"  # Binance: GTX = post-only (Good-Till-Crossing)
        try:
            order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                # 项目 exchange 规范：side 只接受 long/short
                side="short",
                order_type="limit",
                quantity=self.cfg.qty,
                price=price,
                params={
                    "timeInForce": tif,
                    # Hedge 模式：明确标记 SHORT
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_ENTRY",
                },
            )
        except Exception:
            # GTX reject / transient errors => treat as not placed, so caller can retry
            self.state.entry_order_id = None
            self.state.entry_price = None
            return False

        self.state.entry_order_id = order.get("orderId")
        self.state.entry_price = price

        # Track entry chasing state for throttling (only after successful placement)
        self._last_entry_price_sent = float(price)
        self._last_entry_replace_ts = time.time()
        if self._entry_chase_start_ts <= 0:
            self._entry_chase_start_ts = self._last_entry_replace_ts
        return True

    def place_entry_maker_best_ask(self) -> None:
        """Place a post-only entry pegged to best ask (short)."""
        best_ask = self._get_best_ask()
        self.place_entry(best_ask, maker_only=True)

    # ② 空单成交 → 初始化最低价 + 止损
    def on_entry_filled(self, fill_price):
        s = self.state
        s.position_open = True
        s.entry_fill_price = fill_price
        s.lowest_price = fill_price   # ★ 从成交价开始记最低价

        stop_limit = fill_price + self.cfg.stop_limit_distance
        stop_market = stop_limit + self.cfg.stop_market_extra_distance

        # 下止损限价单
        stop_limit_order = self.exchange.create_order(
            symbol=self.cfg.symbol,
            # 平空：用 long + reduceOnly
            side="long",
            # 项目 order_type 规范
            order_type="stop_limit",
            quantity=self.cfg.qty,
            price=stop_limit,
            params={
                "stopPrice": stop_limit,
                "reduceOnly": True,
                "timeInForce": "GTC",
                "positionSide": "SHORT",
                "tag": f"{self.cfg.tag_prefix}_STOP_LIMIT",
            },
        )
        s.stop_limit_order_id = stop_limit_order["orderId"]

        # 下止损市价单
        stop_market_order = self.exchange.create_order(
            symbol=self.cfg.symbol,
            side="long",
            order_type="stop",
            quantity=self.cfg.qty,
            params={
                "stopPrice": stop_market,
                "reduceOnly": True,
                "positionSide": "SHORT",
                "tag": f"{self.cfg.tag_prefix}_STOP_MARKET",
            },
        )
        s.stop_market_order_id = stop_market_order["orderId"]

        # Initialize throttle trackers after first successful placement
        self._last_stop_limit_sent = float(stop_limit)
        self._last_replace_ts = time.time()


    def _should_update_stops(self, *, new_stop_limit: float) -> bool:
        """Throttle stop updates to avoid API rate limits and order spam.

        Both thresholds are configurable on cfg:
          - min_stop_price_delta: minimum change in stop-limit price to trigger an update
          - min_replace_interval_sec: minimum seconds between updates

        If attributes are missing on cfg, safe defaults are used.
        """
        min_price_delta = float(getattr(self.cfg, "min_stop_price_delta", 0.0) or 0.0)
        min_interval = float(getattr(self.cfg, "min_replace_interval_sec", 0.5) or 0.0)

        # Price delta gate
        if self._last_stop_limit_sent is not None:
            if abs(new_stop_limit - self._last_stop_limit_sent) < min_price_delta:
                return False

        # Time delta gate
        now = time.time()
        if (now - self._last_replace_ts) < min_interval:
            return False

        return True

    def on_price(self, price):
        s = self.state

        if not s.position_open:
            return

        # ★ 只在创新低时更新
        if price < s.lowest_price:
            s.lowest_price = price

            new_stop_limit = price + self.cfg.stop_limit_distance
            new_stop_market = new_stop_limit + self.cfg.stop_market_extra_distance

            # Throttle: skip stop updates if change is too small or too frequent
            if not self._should_update_stops(new_stop_limit=float(new_stop_limit)):
                return

            # 先取消旧的止损单
            if s.stop_limit_order_id:
                self.exchange.cancel_order(symbol=self.cfg.symbol, order_id=s.stop_limit_order_id)
            if s.stop_market_order_id:
                self.exchange.cancel_order(symbol=self.cfg.symbol, order_id=s.stop_market_order_id)

            # 重新下止损限价单
            stop_limit_order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                # 平空：用 long + reduceOnly
                side="long",
                # 项目 order_type 规范
                order_type="stop_limit",
                quantity=self.cfg.qty,
                price=new_stop_limit,
                params={
                    "stopPrice": new_stop_limit,
                    "reduceOnly": True,
                    "timeInForce": "GTC",
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_STOP_LIMIT",
                },
            )
            s.stop_limit_order_id = stop_limit_order["orderId"]

            # 重新下止损市价单
            stop_market_order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                side="long",
                order_type="stop",
                quantity=self.cfg.qty,
                params={
                    "stopPrice": new_stop_market,
                    "reduceOnly": True,
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_STOP_MARKET",
                },
            )
            s.stop_market_order_id = stop_market_order["orderId"]

            # Update throttle trackers after successful stop update
            self._last_stop_limit_sent = float(new_stop_limit)
            self._last_replace_ts = time.time()

    # ④ 平仓成交 → 清理 + 下一轮
    def on_exit_filled(self, price):
        s = self.state

        # 取消所有订单
        self.exchange.cancel_all_orders(symbol=self.cfg.symbol)

        # 重置状态（下一轮重新入场）
        s.position_open = False
        s.exit_price = price

        # Clear order/position trackers from the finished cycle
        s.entry_order_id = None
        s.entry_price = None
        s.entry_fill_price = None
        s.lowest_price = None
        s.stop_limit_order_id = None
        s.stop_market_order_id = None

        # Reset entry chasing state for next cycle
        self._last_entry_price_sent = None
        self._last_entry_replace_ts = 0.0
        self._entry_chase_start_ts = 0.0

        # 下一轮入场：改为"启动时第一单"的机制。
        # 如果开启 entry_maker_only，则用 maker-only 贴 best ask 挂单等成交；
        # 否则维持原来的 next_entry_distance 逻辑。
        if bool(getattr(self.cfg, "entry_maker_only", False)):
            # If GTX is rejected / transient failure, on_price_no_position() will retry on next tick.
            self.place_entry_maker_best_ask()
        else:
            next_entry = price - self.cfg.next_entry_distance
            self.place_entry(next_entry)

    # ⑤ 未成交限价单 → 偏离太远就重挂
    def on_price_no_position(self, price):
        s = self.state

        # ✅ Safety net: if we actually have a position but missed a WS FILLED, rebuild state first.
        # This makes the strategy self-healing after WS drops/rebuilds.
        self._sync_position_fallback()
        if s.position_open:
            return

        # Maker-only entry: peg to best ask and chase via cancel+replace.
        if bool(getattr(self.cfg, "entry_maker_only", False)):
            # Always sync local state with exchange OPEN orders first.
            self._sync_entry_open_status()
            try:
                best_ask = self._get_best_ask()
            except Exception:
                return

            # If we have no active entry order yet (or it was cleared), place one.
            if not s.entry_order_id:
                self.place_entry(best_ask, maker_only=True)
                return

            # Throttle cancel+replace
            if not self._should_replace_entry(new_entry_price=float(best_ask)):
                return

            # Reprice if order book moved
            if s.entry_price is None or abs(float(best_ask) - float(s.entry_price)) > 0:
                try:
                    self.exchange.cancel_order(order_id=s.entry_order_id, symbol=self.cfg.symbol)
                except Exception:
                    # If already filled/canceled, ignore and try placing fresh order next tick.
                    s.entry_order_id = None
                    return

                # Re-place at current best ask (GTX). If rejected, we'll retry on next tick.
                self.place_entry(best_ask, maker_only=True)
            return

        # Legacy behavior: 未成交限价单 → 偏离太远就重挂
        if not s.entry_order_id:
            return

        if price > s.entry_price + self.cfg.cancel_distance:
            # 取消旧的入场单
            self.exchange.cancel_order(order_id=s.entry_order_id, symbol=self.cfg.symbol)

            # 重新下入场单
            new_price = price - self.cfg.reentry_distance
            self.place_entry(new_price)