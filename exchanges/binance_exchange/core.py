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
from exchanges.base_exchange import BaseExchange


def _safe_call(cb: Optional[Callable[..., Any]], *args: Any, **kwargs: Any) -> None:
    """Call callback safely (never raise)."""
    if cb is None:
        return
    try:
        cb(*args, **kwargs)
    except Exception:
        # Never let observer crash WS threads.
        logger.warning("[WS_EVENT] callback raised", exc_info=True)


class CoreBinanceExchange(BaseExchange):
    def __init__(self, config, global_config=None):
        """
        初始化币安交易所
        
        Args:
            config: 交易所配置
            global_config: 全局配置
        """
        super().__init__(config, global_config)
        # 保存全局配置（父类已经处理了dry_run）
        self.global_config = global_config or {}
        # WebSocket 启用配置
        self.use_ws = self.global_config.get("use_ws", False)

        # ⭐ 限流相关状态：连续 -1003 次数 & 冷却结束时间戳
        self.consecutive_1003 = 0       # 最近连续出现多少次 -1003
        self.rate_limited_until = 0.0   # 若 > time.time()，说明正处于冷却期

        # ⭐ 行情/持仓缓存：保存最近一次成功从币安获取的真实结果
        # 以格式化后的 symbol 为 key，比如 "ZECUSDC"
        self._last_ticker: Dict[str, Dict] = {}

        # ⭐ 行情缓存更新时间（秒）：来自 WebSocket 或 REST
        self._last_ticker_ts: Dict[str, float] = {}
        # ⭐ 行情缓存新鲜度阈值（秒）：超过则回落到 REST
        self._ws_ticker_max_age: float = float((self.global_config or {}).get("ws_ticker_max_age", 10.0))

        # =========================
        # ⭐ 低噪声 WS 行情日志（可观测但不刷屏）
        # =========================
        # 每个 symbol 最少隔多少秒才允许打印一条 WS 行情日志（心跳）
        self._ws_ticker_log_every_sec: float = float(
            (self.global_config or {}).get("ws_ticker_log_every_sec", 10.0)
        )
        # 价格变化超过多少百分比才额外打印（事件触发），例如 0.001 = 0.2%
        self._ws_ticker_log_min_pct: float = float(
            (self.global_config or {}).get("ws_ticker_log_min_pct", 0.002)
        )
        # 每个 symbol 上一次打印日志的时间戳/价格
        self._ws_ticker_last_log_ts: Dict[str, float] = {}
        self._ws_ticker_last_log_px: Dict[str, float] = {}

        # ⭐ 余额缓存（REST/WS 用）
        self._last_balance: Optional[Dict] = None

        # ⭐ User Data Stream（账户/持仓/订单）缓存：用 websocket 替代 REST 轮询
        self._ws_account_ready: bool = False
        self._ws_lock = threading.RLock()
        # 余额缓存：asset -> {"free": float, "total": float}
        self._ws_balances: Dict[str, Dict[str, float]] = {}
        # 持仓缓存：symbol -> 原始 position dict（ACCOUNT_UPDATE 的 P 列表元素）
        self._ws_positions: Dict[str, Dict[str, Any]] = {}
        # 未成交订单缓存：symbol -> {key(orderId/clientOrderId): order_dict}
        self._ws_open_orders: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._ws_last_update_ts: int = 0

        # ⭐ WS/REST tag 映射
        self._id_to_tag: Dict[str, str] = {}
        # ⭐ 延迟挂单：主单未成交时，不挂 closePosition=True 的 SL
        # key: 主单 clientOrderId（例如 "MANUAL_UI_170..."）
        # val: {"symbol": "...", "positionSide": "...", "stopPrice": "...", "entrySide": "BUY/SELL", "tag": "..."}
        self._pending_stop_losses: Dict[str, Dict[str, Any]] = {}

        # ⭐ 延迟触发后再挂 STOP_LIMIT（两段式）：
        # 先等价格达到 activatePrice，再下真正的 STOP_LIMIT（带 stopPrice+price），用于锁盈/保本。
        # key: 用户自定义 id（通常用 tag 或 clientOrderId），val: config dict
        self._pending_deferred_stop_limits: Dict[str, Dict[str, Any]] = {}

        # ⭐ 方案B：本地触发后再提交订单（通用）
        self._pending_local_trigger_orders: Dict[str, Dict[str, Any]] = {}

        # ✅ pending deferred SL 持久化文件路径（可从 global_config 配）
        self._pending_deferred_sl_path = self.global_config.get(
            "pending_deferred_sl_path",
            os.path.join(os.getcwd(), "pending_deferred_stop_limits.json"),
        )
        # 记录内部用于启动 ticker WS 的 callback，避免重复订阅
        self._internal_deferred_ticker_cbs: Dict[str, Callable] = {}

        # ✅ pending SL 持久化文件路径（可从 global_config 配）
        self._pending_sl_path = self.global_config.get(
            "pending_sl_path",
            os.path.join(os.getcwd(), "pending_stop_losses.json"),
        )

        # ✅ pending local-trigger 持久化文件路径（可从 global_config 配）
        self._pending_local_trigger_path = self.global_config.get(
            "pending_local_trigger_path",
            os.path.join(os.getcwd(), "pending_local_trigger_orders.json"),
        )

        # ✅ 启动时恢复 pending SL（进程重启不丢）
        self._load_pending_stop_losses()
        # ✅ 启动时恢复 deferred STOP_LIMIT（进程重启不丢）
        self._load_pending_deferred_stop_limits()
        # ✅ 启动时恢复本地触发订单（进程重启不丢）
        self._load_pending_local_trigger_orders()

        # ✅ 启动轮询触发线程（不依赖 WS）
        self._local_trigger_poll_stop = threading.Event()
        self._local_trigger_poll_thread = None
        if bool((self.global_config or {}).get("enable_local_trigger_polling", True)):
            self._start_local_trigger_polling_thread()

        # 持仓缓存：key 为 symbol（或 "__ALL__" 表示所有持仓），value 为 positions 列表
        self._last_positions: Dict[str, List[Dict]] = {}

        # ⭐ 杠杆设置去重缓存：避免重复 set_leverage 触发限流
        # key=格式化后的 symbol（如 "ETHUSDC"），value=最近一次成功设置的 leverage
        self._leverage_cache: Dict[str, int] = {}

        # =========================
        # ⭐ 交易规则缓存（tickSize/stepSize）
        # =========================
        # futures_exchange_info 缓存：避免每次精度处理都打 REST，降低 -1003 风险
        self._exinfo_cache: Optional[Dict[str, Any]] = None
        self._exinfo_cache_ts: float = 0.0
        # symbol -> {"tickSize": str|None, "stepSize": str|None}
        self._symbol_rule_cache: Dict[str, Dict[str, Optional[str]]] = {}
        
        # ⭐ WebSocket相关变量
        self.ws_manager = None  # WebSocket管理器
        # 活动的 WebSocket 连接：symbol_fmt -> conn_key
        self.ws_connections: Dict[str, str] = {}
        # 行情 WS 回调：symbol_fmt -> [callback, ...]
        # 允许多个消费者同时订阅同一个 symbol（避免不同 bot 互相覆盖/退订）。
        self.ws_callbacks: Dict[str, List[Callable]] = {}
        self.user_stream_conn_key = None          # 用户流连接 key
        self.user_stream_callback = None          # 上层回调（把订单更新推给策略）
        self._ws_initialized = False  # WebSocket是否已初始化

        # =========================
        # ⭐ User Stream 故障降级：REST 降频 + 保护
        # =========================
        self._user_stream_fail_count: int = 0
        # keepalive 连续失败计数（独立于 subscribe fail）
        self._user_stream_keepalive_fail_count: int = 0
        # 最近一次收到 user stream 事件的时间（time.time() 秒）
        self._user_stream_last_event_ts: float = 0.0
        # 最近一次成功 keepalive 的时间（time.time() 秒）
        self._user_stream_last_keepalive_ok_ts: float = 0.0
        # 防止多线程重复重连
        self._user_stream_rebuild_lock = threading.RLock()
        # health monitor thread
        self._user_stream_health_stop = threading.Event()
        self._user_stream_health_thread = None
        # listenKey（如果能拿到；某些 ws_manager 会自己管理）
        self._user_stream_listen_key: Optional[str] = None
        # 进入降级模式后，至少维持到这个时间点（避免抖动）
        self._user_stream_failed_until: float = 0.0

        # REST 账户类接口（balance/positions/open_orders）节流控制
        # 正常模式下最小间隔（秒）
        self._account_rest_min_interval_normal: float = float(
            (self.global_config or {}).get("account_rest_min_interval_normal", 2.0)
        )
        # 降级模式下最小间隔（秒）：显著降低 REST 频率
        self._account_rest_min_interval_degraded: float = float(
            (self.global_config or {}).get("account_rest_min_interval_degraded", 15.0)
        )

        # 下次允许打 REST 的时间戳（按 endpoint 维度分别控制）
        self._account_rest_next_allowed: Dict[str, float] = {
            "balance": 0.0,
            "positions": 0.0,
            "open_orders": 0.0,
        }
        # 是否处于"账户 REST 降频"模式
        self._account_rest_degraded: bool = False

        # =========================
        # ⭐ WS 事件回调（用于"WS 断线/重连后策略自愈"）
        # =========================
        # 注意：WS 回调线程里会调用该回调；回调内部不要访问 streamlit session_state。
        self._ws_event_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    # -----------------------------    
    # WS event hook (public)
    # -----------------------------    
    def set_ws_event_callback(self, cb: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Register a single WS event callback.

        The callback will be invoked from background threads (keepalive/health/ws callback).
        It must be fast and must not raise.
        """
        self._ws_event_callback = cb

    def _emit_ws_event(self, event: str, **payload: Any) -> None:
        """Emit a small WS event payload for higher layers (dispatcher/bots/UI).

        Payload format (example):
            {"event": "user_stream_rebuild", "stage": "ok", "reason": "...", "ts": 123.45}
        """
        d: Dict[str, Any] = {"event": str(event), "ts": time.time()}
        d.update(payload)
        _safe_call(getattr(self, "_ws_event_callback", None), d)

    # =========================
    # ⭐ Tag 映射工具（核心修复）
    # =========================
    def _register_tag_mapping(self, tag: Optional[str], order: Optional[Dict[str, Any]]) -> None:
        """从 REST 下单返回里记录 id->tag 映射，用于 WS 回报还原策略 tag。"""
        if not tag or not order:
            return
        try:
            t = str(tag)
            candidates = [
                order.get("clientOrderId"),
                order.get("newClientOrderId"),
                order.get("clientAlgoId"),
                order.get("algoId"),
                order.get("orderId"),
                order.get("id"),
            ]
            for c in candidates:
                if c is None:
                    continue
                k = str(c).strip()
                if not k:
                    continue
                self._id_to_tag[k] = t
        except Exception:
            logger.debug("_register_tag_mapping failed", exc_info=True)

    def _resolve_tag_from_ids(
        self,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        client_algo_id: Optional[str] = None,
        algo_id: Optional[str] = None,
    ) -> Optional[str]:
        """用 id->tag 映射还原策略 tag。"""
        for v in (client_order_id, client_algo_id, algo_id, order_id):
            if v is None:
                continue
            k = str(v).strip()
            if not k:
                continue
            if k in self._id_to_tag:
                return self._id_to_tag.get(k)
        return None

    def _rebuild_user_stream(self, reason: str = "") -> bool:
        """Best-effort rebuild user stream.

        IMPORTANT: safe to call from keepalive/health threads.
        """
        if self.dry_run or not getattr(self, "use_ws", False):
            return False

        with getattr(self, "_user_stream_rebuild_lock", threading.RLock()):
            try:
                logger.warning(f"[USER_STREAM] rebuilding... reason={reason}")
                self._emit_ws_event("user_stream_rebuild", stage="start", reason=reason)
                cb = getattr(self, "user_stream_callback", None)
                if cb is None:
                    logger.warning("[USER_STREAM] rebuild skipped: no user_stream_callback")
                    self._emit_ws_event("user_stream_rebuild", stage="skip", reason="no_user_stream_callback")
                    return False
                # hard reset
                try:
                    self.ws_unsubscribe_user_stream()
                except Exception:
                    pass
                # resubscribe (will restart keepalive + monitor)
                ok = bool(self.ws_subscribe_user_stream(cb))
                if ok:
                    logger.warning("[USER_STREAM] rebuild ok")
                    self._emit_ws_event("user_stream_rebuild", stage="ok", reason=reason)
                else:
                    logger.warning("[USER_STREAM] rebuild failed")
                    self._emit_ws_event("user_stream_rebuild", stage="fail", reason=reason)
                return ok
            except Exception:
                logger.error("[USER_STREAM] rebuild exception", exc_info=True)
                self._emit_ws_event("user_stream_rebuild", stage="error", reason=reason)
                return False

    def _start_user_stream_health_monitor(self) -> None:
        """Background monitor: if no user event over M minutes -> health check + reconnect."""
        try:
            # stop previous
            self._stop_user_stream_health_monitor()

            interval_sec = float((self.global_config or {}).get("user_stream_health_check_interval_sec", 15.0))
            interval_sec = max(5.0, interval_sec)

            self._user_stream_health_stop = threading.Event()

            def _run():
                stale_min = float((self.global_config or {}).get("user_stream_stale_min", 5.0))
                while not self._user_stream_health_stop.is_set():
                    if self._user_stream_health_stop.wait(timeout=interval_sec):
                        break
                    try:
                        # Only monitor when WS is enabled and we had a successful subscribe.
                        if not getattr(self, "use_ws", False):
                            continue
                        if not getattr(self, "user_stream_conn_key", None):
                            continue
                        last_evt = float(getattr(self, "_user_stream_last_event_ts", 0.0) or 0.0)
                        if last_evt <= 0:
                            # never received any event yet: don't force rebuild immediately
                            continue
                        age_min = (time.time() - last_evt) / 60.0
                        if age_min > stale_min:
                            # health check: try to rebuild user stream
                            self._rebuild_user_stream(reason=f"stale_no_event_{age_min:.2f}min")
                    except Exception:
                        logger.warning("[USER_STREAM] health monitor tick failed", exc_info=True)

            self._user_stream_health_thread = threading.Thread(
                target=_run,
                daemon=True,
                name="user_stream_health",
            )
            self._user_stream_health_thread.start()
            logger.info(f"[USER_STREAM_HEALTH] started, interval={interval_sec}s")
        except Exception:
            logger.warning("[USER_STREAM_HEALTH] start failed", exc_info=True)

    def _stop_user_stream_health_monitor(self) -> None:
        try:
            stop_evt = getattr(self, "_user_stream_health_stop", None)
            th = getattr(self, "_user_stream_health_thread", None)
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
        except Exception:
            pass
        self._user_stream_health_stop = threading.Event()
        self._user_stream_health_thread = None

    def _start_local_trigger_polling_thread(self):
        """后台轮询：检查 deferred_stop_limit + 本地触发订单是否满足触发条件。"""
        if getattr(self, "_local_trigger_poll_thread", None) is not None:
            return

        # ✅ 修复：默认不要 0.5s 的高频轮询，默认改为 60s（分钟级）
        # 如需更快轮询，用户可显式在 config.yaml 里调小。
        interval = float((self.global_config or {}).get("local_trigger_poll_interval_sec", 60.0))
        # 最小保护：避免 0 或极小值导致 CPU/日志刷屏（仍允许用户显式配置到 >=1s）
        interval = max(1.0, interval)

        def _run():
            logger.info(f"[LOCAL_TRIGGER] polling thread started, interval={interval}s")
            while not self._local_trigger_poll_stop.is_set():
                try:
                    if not self.dry_run:
                        # 一次轮询覆盖所有涉及 symbol
                        self.poll_local_triggers_once()
                except Exception:
                    logger.warning("[LOCAL_TRIGGER] poll tick failed", exc_info=True)
                # 小睡
                try:
                    time.sleep(interval)
                except Exception:
                    pass
            logger.info("[LOCAL_TRIGGER] polling thread stopped")

        th = threading.Thread(target=_run, name="LocalTriggerPoller", daemon=True)
        th.start()
        self._local_trigger_poll_thread = th

    def shutdown(self):
        """可选：如果你项目里有统一 shutdown，就把 stop event 放这里。"""
        try:
            if getattr(self, "_local_trigger_poll_stop", None):
                self._local_trigger_poll_stop.set()
        except Exception:
            pass

        # ✅ 停止 ticker watch 轮询线程（如果启用）
        try:
            if getattr(self, "_ticker_watch_stop", None):
                self._ticker_watch_stop.set()
        except Exception:
            pass

        # ✅ 停止用户流健康监控线程
        try:
            if getattr(self, "_user_stream_health_stop", None):
                self._user_stream_health_stop.set()
        except Exception:
            pass

    def _init_client(self):
        """
        初始化币安客户端
        """
        try:
            # 使用币安官方python-binance库初始化客户端
            kwargs = dict(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
            )

            # 关键修复：没有 proxy 时不要传 proxies=None（python-binance 内部会对 proxies 调 .get()）
            if self.proxy:
                kwargs["requests_params"] = {
                    "proxies": {"http": self.proxy, "https": self.proxy}
                }

            self.client = Client(**kwargs)
            
            logger.info(f"成功初始化币安客户端: {'测试网' if self.testnet else '主网'}")
        except Exception as e:
            logger.error(f"初始化币安客户端失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")

    def _init_ws_client(self):
        """
        初始化WebSocket客户端
        """
        try:
            if self.dry_run:
                logger.info("dry_run模式下不初始化WebSocket客户端")
                return
            
            if not self.use_ws:
                logger.info("use_ws=False，跳过 Binance WebSocket 初始化")
                return
                
            # 初始化WebSocket管理器
            kwargs = {
                "api_key": self.api_key,
                "api_secret": self.api_secret,
            }

            if self.proxy:
                kwargs["https_proxy"] = self.proxy

            # 兼容不同 python-binance 版本：新版本支持就用，不支持就回退
            try:
                kwargs["socket_connect_timeout"] = 15
                self.ws_manager = ThreadedWebsocketManager(**kwargs)
            except TypeError:
                kwargs.pop("socket_connect_timeout", None)
                self.ws_manager = ThreadedWebsocketManager(**kwargs)
            
            # 启动WebSocket管理器
            self.ws_manager.start()
            self._ws_initialized = True
            logger.info(f"成功初始化币安WebSocket客户端: {'测试网' if self.testnet else '主网'}")
        except Exception as e:
            logger.error(f"初始化币安WebSocket客户端失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            self._ws_initialized = False

    def _format_order_type(self, order_type: str) -> str:
        """
        格式化订单类型
        
        Args:
            order_type: 订单类型 (limit/market/stop_limit/stop/trailing_stop/stop_loss_limit/take_profit/take_profit_limit)
            
        Returns:
            格式化后的订单类型（币安合约支持的类型）
        """
        # 币安合约交易的订单类型映射
        order_type_map = {
            "limit": "LIMIT",
            "market": "MARKET",
            "stop_limit": "STOP",               # 止损限价单 - Binance合约API使用 STOP
            "stop": "STOP_MARKET",              # 止损市价单
            "stop_market": "STOP_MARKET",       # 止损市价单
            "trailing_stop": "TRAILING_STOP_MARKET",
            "stop_loss_limit": "STOP",          # 止损限价单
            "take_profit": "TAKE_PROFIT",       # ✅ 新增：止盈触发单
            "take_profit_limit": "TAKE_PROFIT",  # Futures: 用 TAKE_PROFIT + (stopPrice, price) 表示触发后挂限价
        }

        # 转成小写后查表，找不到就默认转大写传出去（尽量接近币安格式）
        key = (order_type or "").lower()
        formatted_type = order_type_map.get(key, (order_type or "").upper())

        # 币安常见有效类型列表，方便日志提示
        valid_order_types = [
            "LIMIT",
            "MARKET",
            "STOP",
            "STOP_MARKET",
            "TRAILING_STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
        ]
        if formatted_type not in valid_order_types:
            logger.warning(f"无效的订单类型: {order_type}, 已转换为: {formatted_type}")

        logger.info(f"订单类型转换: {order_type} -> {formatted_type}")
        return formatted_type

    def _format_symbol(self, symbol: str) -> str:
        """
        格式化交易对
        
        Args:
            symbol: 交易对 (如 BTC/USDT)
            
        Returns:
            格式化后的交易对 (如 BTCUSDT)
        """
        return str(symbol).replace('/', '').upper()

    def _format_side(self, side: str) -> str:
        """
        格式化方向
        
        Args:
            side: 方向 (long/short)
            
        Returns:
            格式化后的方向 (BUY/SELL)
        """
        if side == "long":
            return "BUY"
        elif side == "short":
            return "SELL"
        else:
            return side
