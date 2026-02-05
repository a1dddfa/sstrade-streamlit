# -*- coding: utf-8 -*-
"""
Auto-split from the original binance_exchange.py.
"""
from .deps import (
    logger, trade_logger,
    time, threading, os, json,
    Decimal, ROUND_DOWN,
    Dict, List, Optional, Any, Callable,
    Client, BinanceAPIException, ThreadedWebsocketManager,
)


class WebsocketMixin:
    # ----------------------------- 
    # User stream health / state 
    # ----------------------------- 
    def _compute_user_stream_health(self) -> Dict[str, Any]: 
        """Return a small health snapshot for UI/logging. 

        Status convention: 
        - ws:   ok/degraded/down 
        - rest: ok/degraded (account REST throttled) 
        - overall: ok/degraded/down (worst-of) 
        """
        now = time.time() 
        use_ws = bool(getattr(self, "use_ws", False)) 
        conn = getattr(self, "user_stream_conn_key", None) 

        stale_min = float((self.global_config or {}).get("user_stream_stale_min", 5.0)) 
        last_evt = float(getattr(self, "_user_stream_last_event_ts", 0.0) or 0.0) 
        age_min = (now - last_evt) / 60.0 if last_evt > 0 else 1e9 

        ws_status = "ok" 
        if not use_ws: 
            # WS disabled => treat as ok (REST-only mode) 
            ws_status = "ok" 
        elif not conn: 
            ws_status = "down" 
        elif age_min > stale_min: 
            ws_status = "degraded" 

        rest_status = "degraded" if bool(getattr(self, "_account_rest_degraded", False)) else "ok" 

        overall = "ok" 
        if ws_status == "down": 
            overall = "down" 
        elif ws_status == "degraded" or rest_status == "degraded": 
            overall = "degraded" 

        return { 
            "use_ws": use_ws, 
            "ws": ws_status, 
            "rest": rest_status, 
            "overall": overall, 
            "user_event_age_min": None if last_evt <= 0 else round(age_min, 2), 
            "keepalive_fail_count": int(getattr(self, "_user_stream_keepalive_fail_count", 0) or 0), 
            "subscribe_fail_count": int(getattr(self, "_user_stream_fail_count", 0) or 0), 
            "conn_key": conn, 
        } 

    def get_connection_health(self) -> Dict[str, Any]: 
        """Public method for UI: unified WS/REST/degraded state."""
        return self._compute_user_stream_health()

    def ws_connect(self):
        """
        连接WebSocket
        """
        try:
            if self.dry_run:
                logger.info("dry_run模式下不连接WebSocket")
                return True
            
            if not self.use_ws:
                logger.info("use_ws=False，跳过 WebSocket 连接")
                return True
                
            if not self._ws_initialized:
                self._init_ws_client()
                
            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化")
                return False
            
            logger.info("WebSocket连接成功")
            return True
        except Exception as e:
            logger.error(f"WebSocket连接失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False

    def ws_disconnect(self):
        """
        断开WebSocket连接
        """
        try:
            if self.dry_run:
                logger.info("dry_run模式下不操作WebSocket")
                return True
            
            if not self.use_ws:
                logger.info("use_ws=False，跳过 WebSocket 断开连接")
                return True
                
            if self.ws_manager:
                self._stop_user_stream_keepalive()
                self._stop_user_stream_health_monitor()
                self.ws_manager.stop()
                logger.info("WebSocket断开连接")
                
            # 清理连接和回调
            self.ws_connections.clear()
            self.ws_callbacks.clear()
            self._ws_initialized = False
            return True
        except Exception as e:
            logger.error(f"WebSocket断开连接失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False

    def ws_subscribe_ticker(self, symbol: str, callback: Callable):
        """
        订阅行情数据
        
        Args:
            symbol: 交易对
            callback: 回调函数
        """
        try:
            if self.dry_run:
                logger.info(f"dry_run模式下模拟订阅行情: {symbol}")
                return True
            
            if not self.use_ws:
                logger.info(f"use_ws=False，跳过行情订阅: {symbol}")
                return True
                
            if not self._ws_initialized:
                self._init_ws_client()
                
            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化")
                return False
            
            # 格式化交易对
            symbol_fmt = self._format_symbol(symbol)
            
            # 保存回调（支持同一 symbol 多个订阅者）
            cbs = self.ws_callbacks.get(symbol_fmt)
            if cbs is None:
                cbs = []
                self.ws_callbacks[symbol_fmt] = cbs
            if callback not in cbs:
                cbs.append(callback)

            # 已有连接则直接复用（只增加回调，不重复开 socket）
            if symbol_fmt in self.ws_connections:
                logger.info(f"行情WS已存在，复用连接: {symbol_fmt} (subscribers={len(cbs)})")
                return True
            
            # 订阅行情
            # ⭐ 优先订阅【合约】行情流（futures），避免 REST futures_ticker 仍在轮询
            if hasattr(self.ws_manager, "start_futures_symbol_ticker_socket"):
                conn_key = self.ws_manager.start_futures_symbol_ticker_socket(
                    symbol=symbol_fmt,
                    callback=self._handle_ws_ticker_message
                )
            else:
                # 兼容旧版本 python-binance：没有 futures ticker socket 时退回现货
                conn_key = self.ws_manager.start_symbol_ticker_socket(
                    symbol=symbol_fmt,
                    callback=self._handle_ws_ticker_message
                )
            # 保存连接
            self.ws_connections[symbol_fmt] = conn_key
            logger.info(f"成功订阅行情: {symbol_fmt} (subscribers={len(self.ws_callbacks.get(symbol_fmt, []))})")
            return True
        except Exception as e:
            logger.error(f"订阅行情失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False

    def ws_unsubscribe_ticker(self, symbol: str, callback: Optional[Callable] = None):
        """
        取消订阅行情数据

        Args:
            symbol: 交易对
            callback: 可选，若提供则只移除该订阅者；不提供则全退订
        """
        try:
            if self.dry_run:
                logger.info(f"dry_run模式下模拟取消订阅行情: {symbol}")
                return True
            
            if not self.use_ws:
                logger.info(f"use_ws=False，跳过行情取消订阅: {symbol}")
                return True

            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化")
                return False

            symbol_fmt = self._format_symbol(symbol)
            conn_key = self.ws_connections.get(symbol_fmt)

            # ---------- 1) 先处理回调列表 ----------
            if callback is not None:
                cbs = self.ws_callbacks.get(symbol_fmt) or []
                try:
                    cbs.remove(callback)
                except ValueError:
                    pass

                if cbs:
                    self.ws_callbacks[symbol_fmt] = cbs
                else:
                    self.ws_callbacks.pop(symbol_fmt, None)
            else:
                # 向后兼容：不传 callback => 全退订
                self.ws_callbacks.pop(symbol_fmt, None)

            # ---------- 2) 如果还有订阅者，就不关 socket ----------
            remain = len(self.ws_callbacks.get(symbol_fmt, []))
            if remain > 0:
                logger.info(f"行情WS仍有订阅者，保留连接: {symbol_fmt} (subscribers={remain})")
                return True

            # ---------- 3) 没订阅者了：能关就关 ----------
            if not conn_key:
                self.ws_connections.pop(symbol_fmt, None)
                logger.info(f"行情WS连接不存在，仅清理订阅信息: {symbol_fmt}")
                return True

            self.ws_manager.stop_socket(conn_key)
            logger.info(f"成功取消订阅行情: {symbol_fmt}")

            self.ws_connections.pop(symbol_fmt, None)
            return True

        except Exception as e:
            logger.error(f"取消订阅行情失败: {e}", exc_info=True)
            return False

    def _handle_ws_ticker_message(self, message: Dict):
        """
        处理WebSocket行情消息

        Args:
            message: 行情消息
        """
        try:
            if not message or 's' not in message:
                logger.warning(f"无效的WebSocket行情消息: {message}")
                return

            symbol = message['s']

            # ⭐ 统一字段：REST futures_ticker 用 lastPrice，而 WS ticker 常用 c/p
            ticker = dict(message)
            if "lastPrice" not in ticker:
                if "c" in ticker:  # 24hr ticker stream
                    ticker["lastPrice"] = ticker.get("c")
                elif "p" in ticker:  # price ticker stream
                    ticker["lastPrice"] = ticker.get("p")

            if "bidPrice" not in ticker and "b" in ticker:
                ticker["bidPrice"] = ticker.get("b")
            if "askPrice" not in ticker and "a" in ticker:
                ticker["askPrice"] = ticker.get("a")

            # 更新缓存 + 更新时间
            self._last_ticker[symbol] = ticker
            self._last_ticker_ts[symbol] = time.time()


            # ⭐ deferred STOP_LIMIT: 行情满足 activatePrice 时，触发创建真正的 STOP_LIMIT
            try:
                px0 = float(ticker.get("lastPrice") or 0.0)
                if px0 > 0:
                    self._try_trigger_deferred_stop_limits(symbol, px0)
                    self._try_trigger_local_trigger_orders(symbol, px0)
            except Exception:
                pass
            
            # =========================
            # ⭐ 低噪声 WS 行情日志：心跳 + 阈值触发
            # =========================
            try:
                now = self._last_ticker_ts[symbol]
                px = float(ticker.get("lastPrice") or 0.0)

                last_ts = float(self._ws_ticker_last_log_ts.get(symbol, 0.0))
                last_px = float(self._ws_ticker_last_log_px.get(symbol, 0.0))

                # 心跳：每 N 秒至少打一条
                heartbeat_due = (now - last_ts) >= float(self._ws_ticker_log_every_sec)

                # 阈值：价格相对变化超过 min_pct 打一条（避免刷屏）
                pct_move = 0.0
                if last_px > 0 and px > 0:
                    pct_move = abs(px - last_px) / last_px
                move_due = (pct_move >= float(self._ws_ticker_log_min_pct))

                if (heartbeat_due or move_due) and px > 0:
                    logger.info(
                        "[WS_TICKER] %s last=%s (%.6f) move=%.3f%% age=%.2fs",
                        symbol,
                        ticker.get("lastPrice"),
                        px,
                        pct_move * 100.0,
                        0.0,  # 这里是 WS 刚到的消息，age 约等于 0
                    )
                    self._ws_ticker_last_log_ts[symbol] = now
                    self._ws_ticker_last_log_px[symbol] = px
            except Exception:
                # 低噪声日志失败不影响主流程
                pass

            # 调用回调函数（把规范化后的 ticker 传给上层）
            cbs = self.ws_callbacks.get(symbol) or []
            for cb in list(cbs):
                try:
                    cb(ticker)
                except Exception:
                    # 回调异常不影响 WS 主循环
                    logger.error("ticker 回调异常(已忽略)", exc_info=True)

        except Exception as e:
            logger.error(f"处理WebSocket行情消息失败: {e}", exc_info=True)

    def ws_subscribe_user_stream(self, callback: Callable[[Dict], None]) -> bool:
        """
        订阅用户数据流（包括订单更新、账户变更等）
        """
        # 你可以按需调参：重试次数/间隔/降级保持时间
        max_retries = int((self.global_config or {}).get("user_stream_max_retries", 5))
        retry_sleep = float((self.global_config or {}).get("user_stream_retry_sleep", 2.0))
        degraded_hold_sec = float((self.global_config or {}).get("user_stream_degraded_hold_sec", 300.0))

        try:
            if self.dry_run:
                logger.info("dry_run模式下不订阅用户数据流，直接返回")
                self.user_stream_callback = callback
                return True
            
            if not getattr(self, "use_ws", False):
                logger.info(
                    "[USER_STREAM] use_ws=False，跳过用户数据流订阅（使用 REST 轮询）"
                )
                self.user_stream_callback = callback
                return True

            if not self._ws_initialized:
                self._init_ws_client()

            if self.ws_manager is None:
                logger.warning("[USER_STREAM] ws_manager is None，跳过订阅")
                self._user_stream_fail_count += 1
                self._enter_account_rest_degraded_mode(reason="ws_manager_not_ready", hold_sec=degraded_hold_sec)
                return False

            # 保存上层回调
            self.user_stream_callback = callback

            # 尝试显式获取 futures listenKey（用于 keepalive）。
            # 兼容性策略：
            # - 有 futures_stream_get_listen_key()/futures_get_listen_key 时优先拿到并缓存
            # - 否则保持 None，让 keepalive 线程自行跳过
            try:
                lk = None
                if hasattr(self.client, "futures_stream_get_listen_key"):
                    lk = self.client.futures_stream_get_listen_key()
                elif hasattr(self.client, "futures_get_listen_key"):
                    lk = self.client.futures_get_listen_key()
                if isinstance(lk, dict):
                    lk = lk.get("listenKey")
                if lk:
                    self._user_stream_listen_key = str(lk)
            except Exception:
                # 拿不到 listenKey 也不阻塞订阅
                self._user_stream_listen_key = None

            last_err = None
            for attempt in range(1, max_retries + 1):
                try:
                    # ✅ 关键：如果之前有 conn_key，先判空/清理，避免 stop_socket(None) 或重复 key
                    if self.user_stream_conn_key:
                        try:
                            self.ws_manager.stop_socket(self.user_stream_conn_key)
                        except Exception:
                            pass
                        self.user_stream_conn_key = None

                    # python-binance 版本差异：有的支持 listen_key 参数
                    try:
                        if self._user_stream_listen_key is not None:
                            conn_key = self.ws_manager.start_futures_user_socket(
                                callback=self._handle_ws_user_stream_message,
                                listen_key=self._user_stream_listen_key,
                            )
                        else:
                            conn_key = self.ws_manager.start_futures_user_socket(
                                callback=self._handle_ws_user_stream_message
                            )
                    except TypeError:
                        # 旧版本不支持 listen_key
                        conn_key = self.ws_manager.start_futures_user_socket(
                            callback=self._handle_ws_user_stream_message
                        )

                    # 判空保护：start_xxx 有可能返回 None/空串
                    if not conn_key:
                        raise RuntimeError("start_futures_user_socket returned empty conn_key")

                    self.user_stream_conn_key = conn_key
                    self._user_stream_fail_count = 0  # 订阅成功就清零
                    self._user_stream_keepalive_fail_count = 0
                    logger.info(f"成功订阅用户数据流: conn_key={self.user_stream_conn_key}")
                    # ---- start keepalive thread ----
                    self._start_user_stream_keepalive(self._user_stream_listen_key or "")
                    # ---- start health monitor ----
                    self._start_user_stream_health_monitor()
                    return True

                except Exception as e:
                    last_err = e
                    self._user_stream_fail_count += 1
                    if attempt < max_retries:
                        logger.warning(f"[USER_STREAM] subscribe retry {attempt}/{max_retries}: {e}")
                    else:
                        logger.error(f"[USER_STREAM] subscribe failed attempt={attempt}/{max_retries}: {e}", exc_info=True)
                    time.sleep(retry_sleep)

            # 连续失败：进入降级模式（降低 positions/balance/open_orders 的 REST 频率）
            self._enter_account_rest_degraded_mode(
                reason=f"subscribe_failed_{self._user_stream_fail_count}_times last={type(last_err).__name__}",
                hold_sec=degraded_hold_sec
            )
            return False

        except Exception as e:
            self._user_stream_fail_count += 1
            logger.error(f"订阅用户数据流失败: {e}", exc_info=True)
            self._enter_account_rest_degraded_mode(reason="subscribe_exception", hold_sec=degraded_hold_sec)
            return False

    def _start_user_stream_keepalive(self, listen_key: str) -> None:
        """Start / restart futures user-stream listenKey keepalive thread."""
        try:
            if not listen_key:
                return

            # stop previous keepalive thread if any
            self._stop_user_stream_keepalive()

            interval_sec = int((self.global_config or {}).get("user_stream_keepalive_sec", 30 * 60))
            if interval_sec < 60:
                interval_sec = 60  # safety floor

            self._user_stream_keepalive_stop = threading.Event()
            self._user_stream_keepalive_thread = threading.Thread(
                target=self._user_stream_keepalive_worker,
                args=(listen_key, interval_sec, self._user_stream_keepalive_stop),
                daemon=True,
                name="user_stream_keepalive",
            )
            self._user_stream_keepalive_thread.start()
            logger.info(f"[USER_STREAM_KEEPALIVE] started, interval={interval_sec}s")

        except Exception as e:
            logger.warning(f"[USER_STREAM_KEEPALIVE] start failed: {e}", exc_info=True)

    def _stop_user_stream_keepalive(self) -> None:
        """Stop futures user-stream keepalive thread if running."""
        stop_evt = getattr(self, "_user_stream_keepalive_stop", None)
        th = getattr(self, "_user_stream_keepalive_thread", None)
        if stop_evt is not None:
            try:
                stop_evt.set()
            except Exception:
                pass
        if th is not None and getattr(th, "is_alive", lambda: False)():
            try:
                th.join(timeout=2.0)
            except Exception:
                pass
        self._user_stream_keepalive_stop = None
        self._user_stream_keepalive_thread = None

    def _user_stream_keepalive_worker(self, listen_key: str, interval_sec: int, stop_evt: threading.Event) -> None:
        """Periodically renew futures user-stream listenKey to prevent silent disconnect."""
        max_fail_n = int((self.global_config or {}).get("user_stream_keepalive_fail_n", 3))
        while not stop_evt.is_set():
            if stop_evt.wait(timeout=interval_sec):
                break
            try:
                if not listen_key:
                    logger.debug("[USER_STREAM_KEEPALIVE] listen_key unavailable, skip")
                    continue
                if not getattr(self, "client", None):
                    logger.warning("[USER_STREAM_KEEPALIVE] client is None, skip")
                    continue
                if hasattr(self.client, "futures_stream_keepalive"):
                    self.client.futures_stream_keepalive(listen_key)
                elif hasattr(self.client, "keepalive_futures_stream"):
                    self.client.keepalive_futures_stream(listen_key)
                else:
                    logger.error("[USER_STREAM_KEEPALIVE] no keepalive method found on client")
                    continue

                logger.info("[USER_STREAM_KEEPALIVE] renewed listenKey successfully")
                self._user_stream_keepalive_fail_count = 0
                self._user_stream_last_keepalive_ok_ts = time.time()
            except Exception as e:
                logger.warning(f"[USER_STREAM_KEEPALIVE] renew failed: {e}", exc_info=True)
                try:
                    self._user_stream_keepalive_fail_count = int(getattr(self, "_user_stream_keepalive_fail_count", 0) or 0) + 1
                except Exception:
                    self._user_stream_keepalive_fail_count = 1
                if self._user_stream_keepalive_fail_count >= max_fail_n:
                    # 连续失败 N 次 -> 重建 user stream
                    self._rebuild_user_stream(reason=f"keepalive_failed_{self._user_stream_keepalive_fail_count}_times")

    def ws_unsubscribe_user_stream(self) -> bool:
        """
        取消订阅用户数据流
        """
        try:
            if self.dry_run:
                logger.info("dry_run模式下不取消用户数据流")
                return True
            
            if not self.use_ws:
                logger.info("use_ws=False，跳过用户数据流取消订阅")
                return True

            if self.ws_manager and self.user_stream_conn_key:
                self._stop_user_stream_keepalive()
                self._stop_user_stream_health_monitor()
                self.ws_manager.stop_socket(self.user_stream_conn_key)
                logger.info(f"已取消用户数据流: conn_key={self.user_stream_conn_key}")
                self.user_stream_conn_key = None
                self.user_stream_callback = None
                self._user_stream_listen_key = None
                return True

            logger.warning("当前没有活跃的用户数据流连接")
            return False

        except Exception as e:
            logger.error(f"取消订阅用户数据流失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False

    def _handle_ws_user_stream_message(self, message: Dict):
        """
        处理用户数据流消息（订单更新 + 账户更新）

        Args:
            message: Binance 推送的原始消息
        """
        try:
            if not message:
                logger.warning("收到空的用户数据流消息")
                return
            # ✅ 只要收到有效 message：更新 last event timestamp
            self._user_stream_last_event_ts = time.time()
            # ✅ 只要能收到有效消息，认为 user stream 活着：退出降级
            self._user_stream_fail_count = 0
            self._account_rest_degraded = False
            self._user_stream_failed_until = 0.0

            logger.debug(f"[USER_STREAM_RAW] {message}")
            event_type = message.get("e")

            # ===== 1) ACCOUNT_UPDATE：余额/持仓 =====
            if event_type == "ACCOUNT_UPDATE":
                a = message.get("a", {}) or {}
                balances = a.get("B", []) or []
                positions = a.get("P", []) or []

                with self._ws_lock:                   
                    # 余额：用 walletBalance(wb) 作为 total；用 crossWalletBalance(cw) 作为 free 的简化口径
                    for b in balances:
                        asset = b.get("a")
                        if not asset:
                            continue
                        total = float(b.get("wb", 0.0) or 0.0)
                        free = float(b.get("cw", 0.0) or 0.0)
                        self._ws_balances[str(asset)] = {"free": free, "total": total}

                    # 持仓：按 symbol 缓存原始 dict
                    for p in positions:
                        sym = p.get("s")
                        if not sym:
                            continue
                        self._ws_positions[str(sym)] = p

                    self._ws_account_ready = True
                    self._ws_last_update_ts = int(message.get("E") or (time.time() * 1000))
                return

            # ===== 2) ORDER_TRADE_UPDATE：订单状态 =====
            if event_type != "ORDER_TRADE_UPDATE":
                return

            o = message.get("o", {}) or {}
            if not o:
                logger.warning(f"ORDER_TRADE_UPDATE 中没有 o 字段: {message}")
                return

            order = {
                "symbol":        o.get("s"),
                "side":          o.get("S"),
                "type":          o.get("o"),
                "status":        o.get("X"),
                "execute_type":  o.get("x"),
                "orderId":       o.get("i"),
                "clientOrderId": o.get("c"),
                "avgPrice":      o.get("ap"),
                "price":         o.get("p"),
                "origQty":       o.get("q"),
                "executedQty":   o.get("z"),
                "stopPrice":     o.get("sp"),
                "positionSide":  o.get("ps"),
                "reduceOnly":    o.get("R"),
                "updateTime":    message.get("E"),
            }

            # ===== 还原 tag（策略识别关键字段） =====
            # 1) 常规：clientOrderId 形如 "TAG_170..."，直接取前缀
            # 2) 条件单/算法单：clientOrderId 常被替换成 "x-..."（clientAlgoId）
            #    这时用 REST 下单时记录的映射还原为原始策略 tag（例如 UI_SHORTTRAILSTACK_ENTRY）
            client_id = str(order.get("clientOrderId") or "").strip()
            resolved_tag = None
            if client_id and "_" in client_id:
                resolved_tag = client_id.split("_")[0]
            else:
                resolved_tag = self._resolve_tag_from_ids(
                    client_order_id=client_id or None,
                    order_id=str(order.get("orderId") or "").strip() or None,
                )
                # 如果还是没找到，至少保留原值便于排查
                if resolved_tag is None and client_id:
                    resolved_tag = client_id
            order["tag"] = resolved_tag

            sym = order.get("symbol")
            status = (order.get("status") or "").upper()
            oid = str(order.get("orderId") or "")
            cid = str(order.get("clientOrderId") or "")
            key = oid if oid else cid

            if sym and key:
                with self._ws_lock:
                    if sym not in self._ws_open_orders:
                        self._ws_open_orders[sym] = {}

                    if status in ("NEW", "PARTIALLY_FILLED"):
                        self._ws_open_orders[sym][key] = order
                    elif status in ("FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                        self._ws_open_orders[sym].pop(key, None)
                    else:
                        self._ws_open_orders[sym][key] = order

                    self._ws_account_ready = True
                    self._ws_last_update_ts = int(message.get("E") or (time.time() * 1000))

            logger.info(
                f"[USER_STREAM_ORDER] symbol={order.get('symbol')} status={order.get('status')} "
                f"side={order.get('side')} tag={order.get('tag')} avgPrice={order.get('avgPrice')}"
            )

            trade_logger.info(
                "ORDER_UPDATE | symbol=%s | status=%s | side=%s | orderId=%s | avgPrice=%s | executedQty=%s | tag=%s",
                order.get("symbol"), order.get("status"), order.get("side"),
                order.get("orderId"), order.get("avgPrice"), order.get("executedQty"), order.get("tag"),
            )
            # ⭐ 方案A：只在“主开仓单 FILLED”时触发补挂（避免 TP/SL 自己的 FILLED 误触发）
            otype = (order.get("type") or "").upper()
            reduce_only = bool(order.get("reduceOnly"))

            if status == "FILLED" and (not reduce_only) and otype not in (
                "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"
            ):
                self._try_place_deferred_stop_loss_on_filled(order)

            # ⭐ 主单被取消/拒绝/过期：清掉 pending，避免泄漏
            if status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                if cid:
                    with self._ws_lock:
                        self._pending_stop_losses.pop(cid, None)
                    self._save_pending_stop_losses()

            if self.user_stream_callback:
                self.user_stream_callback(order)

        except Exception as e:
            logger.error(f"处理用户数据流消息失败: {e}", exc_info=True)

    def _get_ws_balance(self, currency: str) -> Optional[Dict[str, float]]:
        with self._ws_lock:
            return self._ws_balances.get(currency)

    def _get_ws_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        with self._ws_lock:
            if symbol:
                sym = self._format_symbol(symbol)
                p = self._ws_positions.get(sym)
                return [p] if p else []
            return list(self._ws_positions.values())

    def _get_ws_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        with self._ws_lock:
            if not symbol:
                out = []
                for mp in self._ws_open_orders.values():
                    out.extend(list(mp.values()))
                return out
            sym = self._format_symbol(symbol)
            return list(self._ws_open_orders.get(sym, {}).values())

    def _wait_main_order_fill_quick(
        self,
        symbol_fmt: str,
        order: Dict[str, Any],
        max_wait_sec: float = 3.0,
        interval_sec: float = 0.5,
    ) -> Dict[str, Any]:
        """
        主单刚下完但回包可能还是 NEW：短轮询等它变 FILLED/有成交，
        用于加速“立刻挂 SL”，避免完全依赖 WS ORDER_TRADE_UPDATE。
        """
        try:
            if self.dry_run:
                return order

            order_id = order.get("orderId")
            client_id = order.get("clientOrderId")

            deadline = time.time() + float(max_wait_sec)
            last = order

            while time.time() < deadline:
                status = str(last.get("status") or "").upper()
                executed = float(last.get("executedQty") or 0.0)

                # 已成交 / 有成交：直接返回
                if status == "FILLED" or executed > 0:
                    return last

                time.sleep(float(interval_sec))

                # 优先用 orderId；没有就用 clientOrderId
                if order_id:
                    last = self.client.futures_get_order(symbol=symbol_fmt, orderId=order_id)
                elif client_id:
                    last = self.client.futures_get_order(symbol=symbol_fmt, origClientOrderId=str(client_id))
                else:
                    break

            return last
        except Exception as e:
            logger.warning(f"[BINANCE] _wait_main_order_fill_quick failed: {e}", exc_info=True)
            return order

    def _defer_stop_loss_until_filled(
        self,
        main_client_order_id: str,
        symbol_fmt: str,
        position_side: str,
        sl_stop_price_precise: str,
        entry_side: str,
        tag: Optional[str] = None,
    ) -> None:
        """
        主单未成交时：暂存 SL，等 WS 里收到该主单 FILLED 再挂 STOP_MARKET + closePosition=True
        """
        if not main_client_order_id:
            # 没有 clientOrderId 就无法可靠关联 WS 的 FILLED 回报
            logger.warning("[BINANCE] defer SL failed: missing main_client_order_id")
            return

        data = {
            "symbol": symbol_fmt,
            "positionSide": position_side,
            "stopPrice": sl_stop_price_precise,
            "entrySide": entry_side,   # BUY or SELL
            "tag": tag,
            "ts": int(time.time() * 1000),
        }

        with self._ws_lock:
            self._pending_stop_losses[main_client_order_id] = data

        # IO 放锁外，缩短临界区
        self._save_pending_stop_losses()

        logger.info(
            f"[BINANCE] DEFER_SL_UNTIL_FILLED | mainClientId={main_client_order_id} | "
            f"symbol={symbol_fmt} | positionSide={position_side} | stopPrice={sl_stop_price_precise} | entrySide={entry_side} | tag={tag}"
        )

    def _try_place_deferred_stop_loss_on_filled(self, filled_order: Dict[str, Any]) -> None:
        """
        WS 收到主单 FILLED 时：如果命中 pending SL，就立即补挂 SL
        """
        cid = str(filled_order.get("clientOrderId") or "")
        if not cid:
            return

        cfg: Optional[Dict[str, Any]] = None
        with self._ws_lock:
            cfg = self._pending_stop_losses.pop(cid, None)

        if cfg:
            # pending 变更就持久化
            self._save_pending_stop_losses()

        if not cfg:
            return  # 没有延迟的 SL

        try:
            symbol_fmt = cfg["symbol"]
            position_side = cfg["positionSide"]
            stop_price = cfg["stopPrice"]
            entry_side = (cfg.get("entrySide") or "").upper()

            # 反向下单：开多(BUY) -> SL 用 SELL；开空(SELL) -> SL 用 BUY
            sl_side = "SELL" if entry_side == "BUY" else "BUY"

            tag = cfg.get("tag") or "DEFER"
            safe_tag = "".join(c if c.isalnum() or c in ["_", "-"] else "_" for c in str(tag))
            sl_client_id = f"{safe_tag}_SL_{int(time.time() * 1000)}"

            logger.info(
                f"[BINANCE] PLACE_DEFERRED_SL | symbol={symbol_fmt} | side={sl_side} | "
                f"type=STOP_MARKET | stopPrice={stop_price} | closePosition=True | positionSide={position_side} | mainClientId={cid}"
            )

            sl_order = self.client.futures_create_order(
                symbol=symbol_fmt,
                side=sl_side,
                type="STOP_MARKET",
                stopPrice=stop_price,
                closePosition=True,
                positionSide=position_side,
                newClientOrderId=sl_client_id,
            )

            logger.info(
                f"[BINANCE] DEFERRED_SL_CREATED | mainClientId={cid} | slOrderId={sl_order.get('orderId')}"
            )

        except Exception as e:
            # ✅ 关键：失败时把 cfg 塞回去，避免 pending 丢失
            logger.error(f"[BINANCE] PLACE_DEFERRED_SL_FAILED: {e}", exc_info=True)
            with self._ws_lock:
                # 防止被别处同时写入，只有不存在时才回填
                if cid not in self._pending_stop_losses:
                    self._pending_stop_losses[cid] = cfg
            self._save_pending_stop_losses()
