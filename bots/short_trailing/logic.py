import time
import logging


# Strategy-specific logger (wired to logs/short_trailing.log by core.logging_config.setup_logging)
log = logging.getLogger("short_trailing")
trade_log = logging.getLogger("trade")


def _safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

class ShortTrailingState:
    position_open = False

    entry_order_id = None
    entry_price = None

    # Entry identifiers (standard vs algo conditional)
    entry_client_id = None
    entry_algo_id = None

    entry_fill_price = None
    lowest_price = None   # ★ 核心：自己维护的最低价

    stop_limit_order_id = None
    stop_market_order_id = None

    # Some Binance conditional/algo orders may not preserve newClientOrderId in user stream.
    # Keep extra identifiers to reliably recognize exit fills.
    stop_limit_client_id = None
    stop_market_client_id = None
    stop_limit_algo_id = None
    stop_market_algo_id = None

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

    def _is_immediate_trigger_reject(self, e: Exception) -> bool:
        """Return True only for exchange rejections meaning the stop would immediately trigger."""
        code = getattr(e, "code", None)
        if code == -2021:
            return True
        msg = str(e).lower()
        return ("would immediately trigger" in msg) or ("immediately trigger" in msg)

    def _force_close_on_stop_limit_reject(self, *, stage: str, ref_price: float) -> None:
        """Policy: any STOP_LIMIT placement/replacement rejection => force-close immediately."""
        log.debug(f"Force close on stop limit reject: stage={stage}, ref_price={ref_price}")
        # Best-effort: cancel all open orders first
        try:
            self.exchange.cancel_all_orders(symbol=self.cfg.symbol)
            log.debug(f"Canceled all orders for {self.cfg.symbol}")
        except Exception as e:
            log.debug(f"Failed to cancel orders: {e}")

        # Market close (reduceOnly) to flatten SHORT position
        try:
            order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                side="long",
                order_type="market",
                quantity=self.cfg.qty,
                params={
                    "reduceOnly": True,
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_FORCE_CLOSE_ON_STOP_LIMIT_REJECT_{stage}",
                },
            )
            log.debug(f"Force close market order placed: {order.get('orderId')}")
        except Exception as e:
            log.debug(f"Failed to place force close order: {e}")
            # If force close fails (rate-limit / exchange down), we still reset local state;
            # resync_from_exchange() can rebuild on the next tick.
            pass

        # Reset local state (mirror on_exit_filled, but without a known exit fill price)
        s = self.state
        s.position_open = False
        s.exit_price = None
        s.entry_order_id = None
        s.entry_price = None
        s.entry_client_id = None
        s.entry_algo_id = None
        s.entry_fill_price = None
        s.lowest_price = None
        s.stop_limit_order_id = None
        s.stop_market_order_id = None

        self._last_stop_limit_sent = None
        self._last_replace_ts = 0.0
        self._last_entry_price_sent = None
        self._last_entry_replace_ts = 0.0
        self._entry_chase_start_ts = 0.0

        # Start next cycle using the same policy as on_exit_filled
        if bool(getattr(self.cfg, "entry_maker_only", False)):
            log.debug("Starting next cycle with maker best ask entry")
            self.place_entry_maker_best_ask()
        else:
            next_entry = float(ref_price) - self.cfg.next_entry_distance
            log.debug(f"Starting next cycle with entry at {next_entry}")
            self.place_entry(next_entry)

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

    def _get_bid_price(self, level: int = 1) -> float:
        """Return bid price at given level from order book (1=best bid, 2=bid2, ...)."""
        ob = self.exchange.get_order_book(self.cfg.symbol, limit=max(5, level + 1))
        bids = (ob or {}).get("bids") or []
        if len(bids) < level:
            raise RuntimeError("order book empty (bids)")
        px = _safe_float(bids[level - 1][0])
        if px is None:
            raise RuntimeError("invalid bid price")
        return float(px)

    def _get_bid2(self) -> float:
        """Convenience: return 2nd best bid (买二)."""
        return self._get_bid_price(level=2)

    @staticmethod
    def _extract_order_refs(order: dict) -> tuple[str | None, str | None, str | None]:
        """Extract (order_id, client_order_id, algo_id) from various Binance responses."""
        if not isinstance(order, dict):
            return None, None, None
        order_id = (order.get("orderId") or order.get("id") or order.get("order_id"))
        client_id = (order.get("clientOrderId") or order.get("origClientOrderId")
                     or order.get("newClientOrderId") or order.get("clientAlgoId"))
        algo_id = (order.get("algoId") or order.get("algoOrderId") or order.get("algo_id"))
        return (
            str(order_id) if order_id is not None else None,
            str(client_id) if client_id is not None else None,
            str(algo_id) if algo_id is not None else None,
        )

    @staticmethod
    def _extract_entry_refs(resp: dict) -> tuple[str|None, str|None, str|None]:
        """Return (order_id, algo_id, client_id) from both standard & algo conditional responses."""
        if not isinstance(resp, dict):
            return None, None, None
        order_id = resp.get("orderId") or resp.get("id")
        algo_id = resp.get("algoId") or resp.get("algoOrderId")
        client_id = resp.get("clientOrderId") or resp.get("clientAlgoId") or resp.get("origClientOrderId")
        return (
            str(order_id) if order_id is not None else None,
            str(algo_id) if algo_id is not None else None,
            str(client_id) if client_id is not None else None,
        )

    def _cancel_entry(self) -> bool:
        """Cancel entry order (standard or algo conditional)."""
        s = self.state
        if getattr(s, "entry_algo_id", None):
            ok = self.exchange.cancel_algo_order(symbol=self.cfg.symbol, algo_id=s.entry_algo_id)
            if ok:
                return True
            # fall through (some wrappers may accept algoId as order_id)
        if getattr(s, "entry_order_id", None) or getattr(s, "entry_client_id", None):
            oid = s.entry_order_id or s.entry_client_id
            return bool(self.exchange.cancel_order(order_id=str(oid), symbol=self.cfg.symbol))
        return False

    def _cancel_entry_any(self) -> bool:
        """Cancel current entry order (standard or algo), best-effort."""
        s = self.state
        if not (s.entry_order_id or s.entry_client_id or s.entry_algo_id):
            return False

        if hasattr(self.exchange, "cancel_order_any"):
            return bool(self.exchange.cancel_order_any(
                symbol=self.cfg.symbol,
                order_id=s.entry_order_id,
                client_order_id=s.entry_client_id,
                algo_id=s.entry_algo_id,
            ))

        if s.entry_order_id or s.entry_client_id:
            oid = s.entry_order_id or s.entry_client_id
            if self.exchange.cancel_order(order_id=oid, symbol=self.cfg.symbol):
                return True

        if s.entry_algo_id and hasattr(self.exchange, "_futures_algo_cancel_order"):
            try:
                return bool(self.exchange._futures_algo_cancel_order(symbol=self.cfg.symbol, algo_id=s.entry_algo_id))
            except Exception:
                return False
        return False

    def _should_replace_entry(self, *, new_entry_price: float) -> bool:
        """Throttle entry cancel+replace when maker-only chasing."""
        min_price_delta = float(getattr(self.cfg, "entry_min_price_delta", 0.0) or 0.0)
        min_interval = float(getattr(self.cfg, "entry_min_replace_interval_sec", 0.2) or 0.0)

        # Price delta gate
        if self._last_entry_price_sent is not None:
            delta = abs(float(new_entry_price) - float(self._last_entry_price_sent))
            if delta < min_price_delta:
                log.debug(
                    "ENTRY_REPLACE_SKIP delta<min | new=%.8f last=%.8f delta=%.8f min_delta=%.8f",
                    float(new_entry_price), float(self._last_entry_price_sent), float(delta), float(min_price_delta),
                )
                return False

        # Time delta gate
        now = time.time()
        dt = float(now - float(self._last_entry_replace_ts or 0.0))
        if dt < min_interval:
            log.debug(
                "ENTRY_REPLACE_SKIP dt<min | dt=%.3fs min_interval=%.3fs last_ts=%.3f",
                float(dt), float(min_interval), float(self._last_entry_replace_ts or 0.0),
            )
            return False

        log.debug(
            "ENTRY_REPLACE_PASS | new=%.8f last=%.8f min_delta=%.8f min_interval=%.3f",
            float(new_entry_price), float(self._last_entry_price_sent or 0.0), float(min_price_delta), float(min_interval),
        )
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
        # algo conditional orders may not appear in get_open_orders()
        if not s.entry_order_id and not s.entry_algo_id:
            return
        if not s.entry_order_id and s.entry_algo_id:
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
        tif = "GTC"  # Entry is STOP_LIMIT; use GTC. (maker_only ignored)
        log.info(
            "ENTRY_PLACE attempt | symbol=%s price=%.8f qty=%s stop=%.8f tag=%s",
            self.cfg.symbol, float(price), str(self.cfg.qty), float(price), f"{self.cfg.tag_prefix}_ENTRY",
        )
        try:
            order = self.exchange.create_order(
                symbol=self.cfg.symbol,
                # 项目 exchange 规范：side 只接受 long/short
                side="short",
                order_type="stop_limit",
                quantity=self.cfg.qty,
                price=price,
                params={
                    "timeInForce": tif,
                    "stopPrice": price,  # trigger price (same as limit)
                    # Hedge 模式：明确标记 SHORT
                    "positionSide": "SHORT",
                    "tag": f"{self.cfg.tag_prefix}_ENTRY",
                },
            )
        except Exception:
            log.exception(
                "ENTRY_PLACE failed | symbol=%s price=%.8f qty=%s",
                self.cfg.symbol, float(price), str(self.cfg.qty),
            )
            self.state.entry_order_id = None
            self.state.entry_price = None
            self.state.entry_client_id = None
            self.state.entry_algo_id = None
            return False

        # === 100% 验证回包结构：跑一次即可确认 orderId vs algoId ===
        log.info("ENTRY_PLACE resp | keys=%s | resp=%s", 
                 list((order or {}).keys()) if isinstance(order, dict) else type(order), 
                 order)

        oid, aid, cid = self._extract_entry_refs(order or {})
        self.state.entry_order_id = oid
        self.state.entry_algo_id = aid
        self.state.entry_client_id = cid
        self.state.entry_price = price

        log.info("ENTRY_PLACE ok | symbol=%s order_id=%s algo_id=%s client_id=%s price=%.8f", 
                 self.cfg.symbol, str(oid), str(aid), str(cid), float(price))
        trade_log.info("SHORTTRAIL_ENTRY_PLACED | symbol=%s order_id=%s price=%.8f", 
                       self.cfg.symbol, str(self.state.entry_order_id), float(price))

        # Track entry chasing state for throttling (only after successful placement)
        self._last_entry_price_sent = float(price)
        self._last_entry_replace_ts = time.time()
        return True

    def place_entry_trigger_bid2(self) -> None:
        """Place an entry STOP_LIMIT (short) with stopPrice=price=bid2."""
        bid2 = self._get_bid2()
        log.info("ENTRY_TRIGGER_BID2 | symbol=%s bid2=%.8f", self.cfg.symbol, float(bid2))
        self.place_entry(bid2, maker_only=False)

    def place_entry_maker_best_ask(self) -> None:
        """Place an entry STOP_LIMIT (short) at best ask price for maker-only."""
        best_ask = self._get_best_ask()
        self.place_entry(best_ask, maker_only=True)

    # ② 空单成交 → 初始化最低价 + 止损
    def on_entry_filled(self, fill_price):
        s = self.state
        log.info("ENTRY_FILLED | symbol=%s fill=%.8f", self.cfg.symbol, float(fill_price))
        trade_log.info("SHORTTRAIL_ENTRY_FILLED | symbol=%s fill=%.8f", self.cfg.symbol, float(fill_price))
        s.position_open = True
        s.entry_fill_price = fill_price
        s.lowest_price = fill_price   # ★ 从成交价开始记最低价

        stop_limit = fill_price + self.cfg.stop_limit_distance
        stop_market = stop_limit + self.cfg.stop_market_extra_distance
        log.debug(f"Setting initial stops: stop_limit={stop_limit}, stop_market={stop_market}")

        # 下止损限价单（失败则立刻强平）
        try:
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
            log.debug(f"Initial stop limit order placed: orderId={stop_limit_order.get('orderId')}, price={stop_limit}")
        except Exception as e:
            log.warning(f"Failed to place initial stop limit order: {e}")
            if self._is_immediate_trigger_reject(e):
                self._force_close_on_stop_limit_reject(stage="INIT", ref_price=float(fill_price))
                return
            self.resync_from_exchange()
            return

        s.stop_limit_order_id = stop_limit_order.get("orderId")
        if not s.stop_limit_order_id:
            log.warning("Initial stop limit order has no orderId; resync instead of force close")
            self.resync_from_exchange()
            return
        s.stop_limit_client_id = (
            stop_limit_order.get("clientOrderId")
            or stop_limit_order.get("clientAlgoId")
            or stop_limit_order.get("tag")
        )
        s.stop_limit_algo_id = stop_limit_order.get("algoId")

        # 下止损市价单（失败则立刻强平；避免裸奔）
        try:
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
            log.debug(f"Initial stop market order placed: orderId={stop_market_order.get('orderId')}, price={stop_market}")
        except Exception as e:
            log.warning(f"Failed to place initial stop market order: {e}")
            if self._is_immediate_trigger_reject(e):
                self._force_close_on_stop_limit_reject(stage="INIT_STOP_MARKET", ref_price=float(fill_price))
                return
            self.resync_from_exchange()
            return

        s.stop_market_order_id = stop_market_order.get("orderId")
        if not s.stop_market_order_id:
            log.warning("Initial stop market order has no orderId; resync instead of force close")
            self.resync_from_exchange()
            return
        s.stop_market_client_id = (
            stop_market_order.get("clientOrderId")
            or stop_market_order.get("clientAlgoId")
            or stop_market_order.get("tag")
        )
        s.stop_market_algo_id = stop_market_order.get("algoId")

        # Initialize throttle trackers after first successful placement
        self._last_stop_limit_sent = float(stop_limit)
        self._last_replace_ts = time.time()
        log.debug("Initial stops placed successfully")

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
            delta = abs(float(new_stop_limit) - float(self._last_stop_limit_sent))
            if delta < min_price_delta:
                log.debug(
                    "STOP_UPDATE_SKIP delta<min | new=%.8f last=%.8f delta=%.8f min_delta=%.8f",
                    float(new_stop_limit), float(self._last_stop_limit_sent), float(delta), float(min_price_delta),
                )
                return False

        # Time delta gate
        now = time.time()
        dt = float(now - float(self._last_replace_ts or 0.0))
        if dt < min_interval:
            log.debug(
                "STOP_UPDATE_SKIP dt<min | dt=%.3fs min_interval=%.3fs last_ts=%.3f",
                float(dt), float(min_interval), float(self._last_replace_ts or 0.0),
            )
            return False

        log.debug(
            "STOP_UPDATE_PASS | new=%.8f last=%.8f min_delta=%.8f min_interval=%.3f",
            float(new_stop_limit), float(self._last_stop_limit_sent or 0.0), float(min_price_delta), float(min_interval),
        )
        return True

    def on_price(self, price):
        log.debug(f"Received price update: {price}")
        s = self.state

        if not s.position_open:
            log.debug("No open position, skipping price update")
            return

        # ★ 只在创新低时更新
        if price < s.lowest_price:
            log.debug(f"New low price: {price} (previous: {s.lowest_price})")
            s.lowest_price = price

            new_stop_limit = price + self.cfg.stop_limit_distance
            new_stop_market = new_stop_limit + self.cfg.stop_market_extra_distance
            log.debug(f"Calculating new stops: stop_limit={new_stop_limit}, stop_market={new_stop_market}")

            # Throttle: skip stop updates if change is too small or too frequent
            if not self._should_update_stops(new_stop_limit=float(new_stop_limit)):
                log.debug("Skipping stop update due to throttle")
                return

            # 先取消旧的止损单
            log.debug("Canceling old stop orders")
            if s.stop_limit_order_id:
                try:
                    self.exchange.cancel_order_any(
                        symbol=self.cfg.symbol, 
                        order_id=s.stop_limit_order_id,
                        algo_id=s.stop_limit_algo_id
                    )
                    log.debug(f"Canceled old stop limit order: {s.stop_limit_order_id}")
                except Exception as e:
                    log.debug(f"Failed to cancel old stop limit order: {e}")
            if s.stop_market_order_id:
                try:
                    self.exchange.cancel_order_any(
                        symbol=self.cfg.symbol, 
                        order_id=s.stop_market_order_id,
                        algo_id=s.stop_market_algo_id
                    )
                    log.debug(f"Canceled old stop market order: {s.stop_market_order_id}")
                except Exception as e:
                    log.debug(f"Failed to cancel old stop market order: {e}")

            # 重新下止损限价单
            try:
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
                log.debug(f"New stop limit order placed: orderId={stop_limit_order.get('orderId')}, price={new_stop_limit}")
            except Exception as e:
                log.warning(f"Failed to place new stop limit order: {e}")
                if self._is_immediate_trigger_reject(e):
                    self._force_close_on_stop_limit_reject(stage="REPLACE", ref_price=float(price))
                    return
                self.resync_from_exchange()
                return

            s.stop_limit_order_id = stop_limit_order.get("orderId")
            if not s.stop_limit_order_id:
                log.warning("New stop limit order has no orderId; resync instead of force close")
                self.resync_from_exchange()
                return
            s.stop_limit_client_id = (
                stop_limit_order.get("clientOrderId")
                or stop_limit_order.get("clientAlgoId")
                or stop_limit_order.get("tag")
            )
            s.stop_limit_algo_id = stop_limit_order.get("algoId")

            # 重新下止损市价单（失败则立刻强平；避免裸奔）
            try:
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
                log.debug(f"New stop market order placed: orderId={stop_market_order.get('orderId')}, price={new_stop_market}")
            except Exception as e:
                log.warning(f"Failed to place new stop market order: {e}")
                if self._is_immediate_trigger_reject(e):
                    self._force_close_on_stop_limit_reject(stage="REPLACE_STOP_MARKET", ref_price=float(price))
                    return
                self.resync_from_exchange()
                return

            s.stop_market_order_id = stop_market_order.get("orderId")
            if not s.stop_market_order_id:
                log.warning("New stop market order has no orderId; resync instead of force close")
                self.resync_from_exchange()
                return
            s.stop_market_client_id = (
                stop_market_order.get("clientOrderId")
                or stop_market_order.get("clientAlgoId")
                or stop_market_order.get("tag")
            )
            s.stop_market_algo_id = stop_market_order.get("algoId")

            # Update throttle trackers after successful stop update
            self._last_stop_limit_sent = float(new_stop_limit)
            self._last_replace_ts = time.time()
            log.debug("Stop orders updated successfully")

    # ④ 平仓成交 → 清理 + 下一轮
    def on_exit_filled(self, price):
        log.debug(f"Exit filled at price={price}")
        s = self.state

        # 取消所有订单
        try:
            self.exchange.cancel_all_orders(symbol=self.cfg.symbol)
            log.debug(f"Canceled all orders for {self.cfg.symbol}")
        except Exception as e:
            log.debug(f"Failed to cancel all orders: {e}")

        # 重置状态（下一轮重新入场）
        s.position_open = False
        s.exit_price = price
        log.debug("Resetting state for next cycle")

        # Clear order/position trackers from the finished cycle
        s.entry_order_id = None
        s.entry_price = None
        s.entry_client_id = None
        s.entry_algo_id = None
        s.entry_fill_price = None
        s.lowest_price = None
        s.stop_limit_order_id = None
        s.stop_market_order_id = None
        s.stop_limit_client_id = None
        s.stop_market_client_id = None
        s.stop_limit_algo_id = None
        s.stop_market_algo_id = None

        # Reset entry chasing state for next cycle
        self._last_entry_price_sent = None
        self._last_entry_replace_ts = 0.0

        # 下一轮入场：改为"启动时第一单"的机制。
        # 出场后下一轮入场：继续用 bid2 触发限价单；
        # If transient failure, on_price_no_position() will retry on next tick.
        log.debug("Placing next entry order")
        self.place_entry_trigger_bid2()

    # ⑤ 未成交限价单 → 偏离太远就重挂
    def on_price_no_position(self, price):
        s = self.state

        log.debug(
            "TICK_NO_POSITION | symbol=%s ref_price=%.8f entry_id=%s entry_px=%s",
            self.cfg.symbol, float(price), str(s.entry_order_id), str(s.entry_price),
        )

        # ✅ Safety net: if we actually have a position but missed a WS FILLED, rebuild state first.
        self._sync_position_fallback()
        if s.position_open:
            return

        # Entry: STOP_LIMIT (short). triggerPrice == limitPrice == bid2 (买二).
        # Keep the existing throttle knobs to avoid excessive cancel+replace.
        self._sync_entry_open_status()
        try:
            bid2 = self._get_bid2()
        except Exception:
            log.exception("GET_BID2 failed | symbol=%s", self.cfg.symbol)
            return

        log.debug(
            "ENTRY_BOOK | symbol=%s bid2=%.8f last_sent=%s cfg_min_delta=%s cfg_min_interval=%s",
            self.cfg.symbol,
            float(bid2),
            str(self._last_entry_price_sent),
            str(getattr(self.cfg, "entry_min_price_delta", None)),
            str(getattr(self.cfg, "entry_min_replace_interval_sec", None)),
        )

        # If we have no active entry order yet (or it was cleared), place one.
        if not s.entry_order_id and not s.entry_algo_id:
            self.place_entry(bid2, maker_only=False)
            return

        # Throttle cancel+replace
        if not self._should_replace_entry(new_entry_price=float(bid2)):
            return

        # Reprice if order book moved
        if s.entry_price is None or abs(float(bid2) - float(s.entry_price)) > 0:
            try:
                log.info("ENTRY_REPLACE cancel | symbol=%s order_id=%s algo_id=%s old_px=%s new_px=%.8f",
                         self.cfg.symbol, str(s.entry_order_id), str(getattr(s, "entry_algo_id", None)),
                         str(s.entry_price), float(bid2))
                if not self._cancel_entry():
                    raise RuntimeError("cancel_entry_failed")
            except Exception:
                log.exception("ENTRY_REPLACE cancel_failed | symbol=%s", self.cfg.symbol)
                s.entry_order_id = None
                s.entry_algo_id = None
                s.entry_client_id = None
                return

            # Re-place at current bid2.
            ok = self.place_entry(bid2, maker_only=False)
            if ok:
                trade_log.info(
                    "SHORTTRAIL_ENTRY_REPLACED | symbol=%s new_px=%.8f",
                    self.cfg.symbol, float(bid2),
                )
        return
