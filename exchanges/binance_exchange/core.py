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
        # 是否处于“账户 REST 降频”模式
        self._account_rest_degraded: bool = False

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
            self.ws_manager = ThreadedWebsocketManager(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
                https_proxy=self.proxy
            )
            
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
            "take_profit_limit": "TAKE_PROFIT_LIMIT",  # 止盈限价单
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
            "TAKE_PROFIT_LIMIT",   # ✅ 补充这一项，避免误报 warning
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
