# -*- coding: utf-8 -*-
"""
币安交易所实现
"""
import logging
import time  # ⭐ 新增这一行
import threading
from typing import Dict, List, Optional, Any, Callable
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.ws.streams import ThreadedWebsocketManager
from exchanges.base_exchange import BaseExchange

logger = logging.getLogger(__name__)


class BinanceExchange(BaseExchange):
    """
    币安交易所实现类
    """
    
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

        # 全局开关：项目只使用轮询（默认 False）
        self.use_ws = bool((global_config or {}).get("use_ws", False))

        # ⭐ 限流相关状态：连续 -1003 次数 & 冷却结束时间戳
        self.consecutive_1003 = 0       # 最近连续出现多少次 -1003
        self.rate_limited_until = 0.0   # 若 > time.time()，说明正处于冷却期

        # ⭐ 行情/持仓缓存：保存最近一次成功从币安获取的真实结果
        # 以格式化后的 symbol 为 key，比如 "ZECUSDC"
        self._last_ticker: Dict[str, Dict] = {}

        # ⭐ 行情缓存更新时间（秒）：来自 WebSocket 或 REST
        self._last_ticker_ts: Dict[str, float] = {}
        # ⭐ 行情缓存新鲜度阈值（秒）：超过则回落到 REST
        self._ws_ticker_max_age: float = 2.0

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
        # 持仓缓存：key 为 symbol（或 "__ALL__" 表示所有持仓），value 为 positions 列表
        self._last_positions: Dict[str, List[Dict]] = {}

        # ⭐ 杠杆设置去重缓存：避免重复 set_leverage 触发限流
        # key=格式化后的 symbol（如 "ETHUSDC"），value=最近一次成功设置的 leverage
        self._leverage_cache: Dict[str, int] = {}
        
        # ⭐ WebSocket相关变量
        self.ws_manager = None  # WebSocket管理器
        self.ws_connections = {}  # 活动的WebSocket连接
        self.ws_callbacks = {}  # WebSocket消息回调函数
        self.user_stream_conn_key = None          # 用户流连接 key
        self.user_stream_callback = None          # 上层回调（把订单更新推给策略）
        self._ws_initialized = False  # WebSocket是否已初始化
    
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
    
    def ws_connect(self):
        """
        连接WebSocket
        """
        try:
            if not getattr(self, "use_ws", False):
                # 项目只用轮询：禁止任何 WS 连接建立
                return
            if self.dry_run:
                logger.info("dry_run模式下不连接WebSocket")
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
            if not getattr(self, "use_ws", False):
                return
            if self.dry_run:
                logger.info("dry_run模式下不操作WebSocket")
                return True
                
            if self.ws_manager:
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
            if not getattr(self, "use_ws", False):
                # no-op unsubscribe
                return lambda: None
            if self.dry_run:
                logger.info(f"dry_run模式下模拟订阅行情: {symbol}")
                return True
                
            if not self._ws_initialized:
                self._init_ws_client()
                
            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化")
                return False
            
            # 格式化交易对
            symbol_fmt = self._format_symbol(symbol)
            
            # 保存回调
            self.ws_callbacks[symbol_fmt] = callback
            
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
            logger.info(f"成功订阅行情: {symbol_fmt}")
            return True
        except Exception as e:
            logger.error(f"订阅行情失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False
    
    def ws_subscribe_user_stream(self, callback: Callable[[Dict], None]) -> bool:
        """
        订阅用户数据流（包括订单更新、账户变更等）

        Args:
            callback: 当有订单更新时回调函数，参数是统一格式的订单字典
        """
        try:
            if not getattr(self, "use_ws", False):
                return lambda: None
            if self.dry_run:
                logger.info("dry_run模式下不订阅用户数据流，直接返回")
                self.user_stream_callback = callback
                return True

            if not self._ws_initialized:
                self._init_ws_client()

            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化，无法订阅用户数据流")
                return False

            # 保存上层回调
            self.user_stream_callback = callback

            # 启动永续合约用户数据流（python-binance 会内部处理 listenKey）
            self.user_stream_conn_key = self.ws_manager.start_futures_user_socket(
                callback=self._handle_ws_user_stream_message
            )

            logger.info(f"成功订阅用户数据流: conn_key={self.user_stream_conn_key}")
            return True

        except Exception as e:
            logger.error(f"订阅用户数据流失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
            return False

    def ws_unsubscribe_user_stream(self) -> bool:
        """
        取消订阅用户数据流
        """
        try:
            if not getattr(self, "use_ws", False):
                return
            if self.dry_run:
                logger.info("dry_run模式下不取消用户数据流")
                return True

            if self.ws_manager and self.user_stream_conn_key:
                self.ws_manager.stop_socket(self.user_stream_conn_key)
                logger.info(f"已取消用户数据流: conn_key={self.user_stream_conn_key}")
                self.user_stream_conn_key = None
                self.user_stream_callback = None
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

            # 从 clientOrderId 里还原 tag（例如 "A1_170..." -> "A1"）
            client_id = order.get("clientOrderId") or ""
            order["tag"] = client_id.split("_")[0] if client_id else None

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

            if self.user_stream_callback:
                self.user_stream_callback(order)

        except Exception as e:
            logger.error(f"处理用户数据流消息失败: {e}", exc_info=True)


    def ws_unsubscribe_ticker(self, symbol: str):
        """
        取消订阅行情数据
        
        Args:
            symbol: 交易对
        """
        try:
            if not getattr(self, "use_ws", False):
                return
            if self.dry_run:
                logger.info(f"dry_run模式下模拟取消订阅行情: {symbol}")
                return True
                
            if not self.ws_manager:
                logger.error("WebSocket管理器未初始化")
                return False
            
            # 格式化交易对
            symbol_fmt = self._format_symbol(symbol)
            
            # 获取连接key
            conn_key = self.ws_connections.get(symbol_fmt)
            if conn_key:
                # 取消订阅
                self.ws_manager.stop_socket(conn_key)
                logger.info(f"成功取消订阅行情: {symbol_fmt}")
                
                # 清理连接和回调
                del self.ws_connections[symbol_fmt]
                if symbol_fmt in self.ws_callbacks:
                    del self.ws_callbacks[symbol_fmt]
                return True
            else:
                logger.warning(f"未找到行情订阅: {symbol_fmt}")
                return False
        except Exception as e:
            logger.error(f"取消订阅行情失败: {e}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")
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

            # 调用回调函数（把规范化后的 ticker 传给上层）
            if symbol in self.ws_callbacks:
                cb = self.ws_callbacks[symbol]
                cb(ticker)

        except Exception as e:
            logger.error(f"处理WebSocket行情消息失败: {e}", exc_info=True)


    # =========================
    # ⭐ 限流保护相关工具方法
    # =========================
    def _is_in_rate_limit_cooldown(self) -> bool:
        """
        检查当前是否处于限流冷却期。
        如果在冷却期内，则返回 True，上层应该跳过本次对币安的真实请求。
        """
        if self.rate_limited_until <= 0:
            return False

        now = time.time()
        if now < self.rate_limited_until:
            remaining = int(self.rate_limited_until - now)
            logger.warning(f"[RATE LIMIT] 冷却期中，还需 {remaining}s，跳过 Binance API 调用")
            return True

        # 冷却期结束，重置
        self.rate_limited_until = 0.0
        self.consecutive_1003 = 0
        return False

    # =========================
    # ⭐ User Data Stream 缓存读取
    # =========================
    def _get_ws_balance(self, currency: str) -> Optional[Dict[str, float]]:
        with self._ws_lock:
            return self._ws_balances.get(currency)

    def _get_ws_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        if not getattr(self, "use_ws", False):
            return []
        with self._ws_lock:
            if symbol:
                sym = self._format_symbol(symbol)
                p = self._ws_positions.get(sym)
                return [p] if p else []
            return list(self._ws_positions.values())

    def _get_ws_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        if not getattr(self, "use_ws", False):
            return []
        with self._ws_lock:
            if not symbol:
                out = []
                for mp in self._ws_open_orders.values():
                    out.extend(list(mp.values()))
                return out
            sym = self._format_symbol(symbol)
            return list(self._ws_open_orders.get(sym, {}).values())
        now = time.time()
        if now < self.rate_limited_until:
            remaining = int(self.rate_limited_until - now)
            logger.warning(
                f"[RATE LIMIT] 当前处于限流冷却期，还需 {remaining} 秒，跳过本次 Binance API 调用"
            )
            return True
        else:
            # 冷却期已过，重置标记
            self.rate_limited_until = 0.0
            self.consecutive_1003 = 0
            return False

    def _handle_rate_limit_error(self, e: Exception, context: str = "") -> None:
        """
        处理币安限流错误（特别是 -1003），并在连续多次出现时进入冷却期。
        
        Args:
            e: 捕获到的异常
            context: 出错时所在的方法上下文描述，方便日志排查
        """
        # 只有 BinanceAPIException 且 code 为 -1003 时才做特别处理
        if isinstance(e, BinanceAPIException) and getattr(e, "code", None) == -1003:
            self.consecutive_1003 += 1
            logger.error(
                f"[RATE LIMIT] 在 {context} 中收到 Binance -1003 (第 {self.consecutive_1003} 次): {e}"
            )

            # ⭐ 连续 3 次 -1003，则进入 60 秒冷却期（你可以按需要调整阈值和时间）
            if self.consecutive_1003 >= 3:
                cooldown_seconds = 60
                self.rate_limited_until = time.time() + cooldown_seconds
                logger.error(
                    f"[RATE LIMIT] 连续 {self.consecutive_1003} 次 -1003，"
                    f"进入 {cooldown_seconds} 秒冷却期，期间将跳过对 Binance 的真实请求"
                )
        else:
            # 其他错误或正常返回时，重置计数
            self.consecutive_1003 = 0
    
    def get_balance(self, currency: str = "USDT") -> Dict:
        """
        获取账户余额（优先使用 User Data Stream 缓存，必要时才走 REST）。

        Args:
            currency: 货币类型 (默认 USDT)

        Returns:
            余额信息字典（与测试兼容的格式）
        """
        # 0) WS 已就绪：优先返回 WS 缓存余额（不走 REST）
        if not self.dry_run and getattr(self, "_ws_account_ready", False):
            b = self._get_ws_balance(currency)
            if b is not None:
                total = float(b.get("total", 0.0) or 0.0)
                free = float(b.get("free", 0.0) or 0.0)
                used = total - free
                result = {
                    currency: {"free": free, "used": used, "total": total},
                    "totalWalletBalance": total,
                }
                self._last_balance = result
                return result

        # 1) 冷却期：不要打 REST，返回缓存（没有缓存就返回 0）
        if self._is_in_rate_limit_cooldown():
            if self._last_balance is not None:
                logger.warning("[RATE LIMIT] 冷却期内 get_balance 返回缓存余额")
                return self._last_balance
            logger.warning("[RATE LIMIT] 冷却期内 get_balance 无缓存，返回 0 余额（上层应跳过交易）")
            return {currency: {"free": 0.0, "used": 0.0, "total": 0.0}, "totalWalletBalance": 0.0}

        try:
            if self.dry_run:
                return {
                    f"{currency}": {"free": 10000.0, "used": 0.0, "total": 10000.0},
                    "totalWalletBalance": 10000.0,
                }

            balance_list = self.client.futures_account_balance()
            currency_balance = next((b for b in balance_list if b.get("asset") == currency), None)

            if currency_balance:
                free = float(currency_balance.get("availableBalance", "0.0") or 0.0)
                total = float(currency_balance.get("balance", "0.0") or 0.0)
                used = total - free
                result = {currency: {"free": free, "used": used, "total": total}, "totalWalletBalance": total}
            else:
                result = {currency: {"free": 0.0, "used": 0.0, "total": 0.0}, "totalWalletBalance": 0.0}

            self.consecutive_1003 = 0
            self._last_balance = result
            return result

        except Exception as e:
            self._handle_rate_limit_error(e, context="get_balance")
            logger.error(f"获取账户余额失败: {e}", exc_info=True)

            if self._last_balance is not None:
                logger.warning("get_balance 失败，返回缓存余额")
                return self._last_balance

            return {currency: {"free": 0.0, "used": 0.0, "total": 0.0}, "totalWalletBalance": 0.0}

    
    def get_ticker(self, symbol: str) -> Dict:
        """
        获取行情信息
        
        Args:
            symbol: 交易对
            
        Returns:
            行情信息字典
        """
        symbol_fmt = self._format_symbol(symbol)

        # ⭐ 0. 优先使用 WebSocket/缓存行情（只要足够新鲜，就不打 REST）
        cached = self._last_ticker.get(symbol_fmt)
        cached_ts = self._last_ticker_ts.get(symbol_fmt, 0.0)
        if cached is not None:
            try:
                price = float(cached.get('lastPrice', 0.0) or 0.0)
            except Exception:
                price = 0.0
            if price > 0 and (time.time() - cached_ts) <= self._ws_ticker_max_age:
                return cached

        # ⭐ 1. 如果当前处于限流冷却期，优先返回缓存的真实行情
        if self._is_in_rate_limit_cooldown():
            cached = self._last_ticker.get(symbol_fmt)
            if cached is not None:
                logger.warning(f"[RATE LIMIT] 冷却期内 get_ticker({symbol_fmt}) 使用缓存行情数据")
                return cached

            logger.error(
                   f"[RATE LIMIT] 冷却期内 get_ticker({symbol_fmt}) 无缓存，且不能返回模拟价，"
                   f"上层应跳过交易逻辑"
            )
            raise RuntimeError(f"限流冷却期且无缓存：无法获取 {symbol_fmt} 行情")

        try:
            if self.dry_run:
                # 模拟行情数据（测试模式）
                return {
                    'symbol': symbol_fmt,
                    'priceChange': '0.0',
                    'priceChangePercent': '0.0',
                    'weightedAvgPrice': '45000.0',
                    'prevClosePrice': '45000.0',
                    'lastPrice': '45000.0',
                    'lastQty': '0.01',
                    'bidPrice': '44999.0',
                    'bidQty': '0.1',
                    'askPrice': '45001.0',
                    'askQty': '0.1',
                    'openPrice': '45000.0',
                    'highPrice': '46000.0',
                    'lowPrice': '44000.0',
                    'volume': '10000.0',
                    'quoteVolume': '450000000.0',
                    'openTime': 1672531200000,
                    'closeTime': 1672617600000,
                    'firstId': 100000000,
                    'lastId': 100010000,
                    'count': 10000,
                }

            logger.info(f"尝试获取行情信息: symbol={symbol_fmt}, proxy={self.proxy}")
            # 使用python-binance库获取24小时行情
            ticker = self.client.futures_ticker(symbol=symbol_fmt)
            logger.info(f"获取行情信息成功: {ticker}")

            # ⭐ 调用成功：重置连续 -1003 计数，并更新缓存
            self.consecutive_1003 = 0
            self._last_ticker[symbol_fmt] = ticker
            self._last_ticker_ts[symbol_fmt] = time.time()

            return ticker

        except Exception as e:
            # ⭐ 出错：先交给限流逻辑处理 -1003
            self._handle_rate_limit_error(e, context="get_ticker")

            logger.error(f"获取行情信息失败: {e}")
            logger.error(f"错误类型: {type(e).__name__}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")

            # ⭐ 优先返回缓存的真实数据
            cached = self._last_ticker.get(symbol_fmt)
            if cached is not None:
                logger.warning(f"获取行情失败，get_ticker({symbol_fmt}) 返回缓存行情数据")
                return cached

            # ✅ 改成：要么抛异常，要么返回明显的“无效价格”
            logger.error(f"获取行情失败且无缓存，get_ticker({symbol_fmt}) 无法返回有效行情")
            raise RuntimeError(f"无法获取 {symbol_fmt} 的真实行情")
    
    def get_order_book(self, symbol: str, limit: int = 10) -> Dict:
        """
        获取订单簿
        
        Args:
            symbol: 交易对
            limit: 获取数量
            
        Returns:
            订单簿信息字典
        """
        try:
            if self.dry_run:
                # 模拟订单簿数据
                symbol = self._format_symbol(symbol)
                base_price = 45000.0
                asks = []
                bids = []
                
                for i in range(limit):
                    # 模拟卖盘
                    ask_price = base_price + (i + 1) * 10.0
                    ask_qty = 0.5 + (i * 0.1)
                    asks.append([str(ask_price), str(ask_qty)])
                    # 模拟买盘
                    bid_price = base_price - (i + 1) * 10.0
                    bid_qty = 0.5 + (i * 0.1)
                    bids.append([str(bid_price), str(bid_qty)])
                
                return {
                    'lastUpdateId': 100000000,
                    'E': 1672531200000,
                    'T': 1672531200000,
                    'asks': asks,
                    'bids': bids
                }
            
            symbol = self._format_symbol(symbol)
            # 使用python-binance库获取订单簿
            order_book = self.client.futures_order_book(symbol=symbol, limit=limit)
            return order_book
        except Exception as e:
            logger.error(f"获取订单簿失败: {e}")
            # 返回模拟数据作为后备
            symbol = self._format_symbol(symbol)
            base_price = 45000.0
            asks = []
            bids = []
            
            for i in range(min(limit, 10)):
                asks.append([str(base_price + (i + 1) * 10.0), str(0.5 + (i * 0.1))])
                bids.append([str(base_price - (i + 1) * 10.0), str(0.5 + (i * 0.1))])
            
            return {
                'lastUpdateId': 100000000,
                'E': 1672531200000,
                'T': 1672531200000,
                'asks': asks,
                'bids': bids
            }

    def _futures_algo_create_order(self, api_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Binance USDⓈ-M Futures Algo conditional order
        正确 endpoint: POST /fapi/v1/algoOrder
        """
        try:
            # 给 signed endpoint 兜底补 timestamp/recvWindow（避免库版本差异）
            import time
            api_params = dict(api_params)
            api_params.setdefault("recvWindow", 5000)
            api_params.setdefault("timestamp", int(time.time() * 1000))

            # ✅ 关键：这里必须是 "algoOrder"（不是 "algo/order"）
            result = self.client._request_futures_api(
                method="post",
                path="algoOrder",
                signed=True,
                data=api_params
            )
            logger.info(f"币安API返回Algo订单: {result}")
            return result
        except Exception as e:
            logger.error(f"创建Algo订单失败: {e}")
            raise

    def create_order(self, symbol: str, side: str, order_type: str,
                     quantity: float, price: Optional[float] = None,
                     params: Optional[Dict] = None) -> Dict:
        """
        创建订单

        Args:
            symbol: 交易对
            side: 方向 (long/short)
            order_type: 订单类型 (limit/market/stop_limit/trailing_stop)
            quantity: 数量
            price: 价格
            params: 其他参数

        Returns:
            订单信息字典
        """
        try:
            # ========== 1. DRY RUN 分支 ==========
            if self.dry_run:
                # 模拟创建订单
                symbol_fmt = self._format_symbol(symbol)
                side_fmt = self._format_side(side)
                type_fmt = self._format_order_type(order_type)

                position_side = params.get('positionSide', 'BOTH') if params else 'BOTH'
                stop_price = params.get('stopPrice', '0.0') if params else '0.0'

                order = {
                    'orderId': str(int(time.time() * 1000)),
                    'symbol': symbol_fmt,
                    'status': 'NEW',
                    'clientOrderId': f'dry_{int(time.time() * 1000)}',
                    'price': str(price or 0.0),
                    'avgPrice': '0.0',
                    'origQty': str(quantity),
                    'executedQty': '0.0',
                    'cumQuote': '0.0',
                    'timeInForce': 'GTC',
                    'type': type_fmt,
                    'reduceOnly': False,
                    'closePosition': False,
                    'side': side_fmt,
                    'positionSide': position_side,
                    'stopPrice': stop_price,
                    'workingType': 'CONTRACT_PRICE',
                    'priceProtect': False,
                    'origType': type_fmt,
                    'time': int(time.time() * 1000),
                    'updateTime': int(time.time() * 1000)
                }

                # 把策略传进来的 tag 附加回订单对象，方便 on_order_update 使用
                if params and 'tag' in params:
                    order['tag'] = params['tag']

                return order

            # ========== 2. 实盘分支：先处理参数 ==========
            symbol_fmt = self._format_symbol(symbol)
            side_fmt = self._format_side(side)
            type_fmt = self._format_order_type(order_type)

            processed_params = (params or {}).copy()

            # 统一处理 stop_price / stopPrice
            stop_price = processed_params.get('stop_price') or processed_params.get('stopPrice')
            if stop_price is not None:
                processed_params['stopPrice'] = stop_price
                if 'stop_price' in processed_params:
                    del processed_params['stop_price']

            # 某些类型需要 stopPrice，但你当前策略主要用 STOP / TAKE_PROFIT / STOP_LOSS_LIMIT，这里保留占位逻辑
            if type_fmt in ['STOP', 'TAKE_PROFIT', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']:
                _has_stop_price = 'stopPrice' in processed_params
                _position_side = processed_params.get('positionSide', 'LONG')
                # 这里暂时不做额外转换，只是保留结构

            # 精度处理（注意：如果以后用到纯市价单，这里要加 price 为 None 的判断）
            quantity_precise = self.amount_to_precision(symbol_fmt, quantity)
            price_precise = self.price_to_precision(symbol_fmt, price) if price is not None else None

            if 'stopPrice' in processed_params:
                processed_params['stopPrice'] = self.price_to_precision(symbol_fmt, processed_params['stopPrice'])

            # 构造主订单参数
            order_params: Dict[str, Any] = {
                'symbol': symbol_fmt,
                'side': side_fmt,
                'type': type_fmt,
                'quantity': quantity_precise,
                'timeInForce': 'GTC',
                **processed_params
            }
            if price_precise is not None:
                order_params['price'] = price_precise

            # 默认 positionSide
            if 'positionSide' not in order_params:
                order_params['positionSide'] = 'LONG' if side_fmt == 'BUY' else 'SHORT'
            
            # ⭐ 如果上层传了 tag，用它生成 newClientOrderId，方便后续通过 clientOrderId 识别策略订单
            tag_value = processed_params.get('tag')
            if tag_value:
                # 简单做一下字符清洗（只保留字母数字和下划线/中划线）
                safe_tag = ''.join(
                    c if c.isalnum() or c in ['_', '-'] else '_' 
                    for c in str(tag_value)
                )
                client_id = f"{safe_tag}_{int(time.time() * 1000)}"
                order_params['newClientOrderId'] = client_id
            
            # ⭐ 对 MARKET / STOP_MARKET 不要传 timeInForce，避免报错
            if type_fmt in ['MARKET', 'STOP_MARKET']:
                order_params.pop('timeInForce', None)

            logger.info(
                f"[BINANCE] CREATE_MAIN_ORDER | symbol={symbol_fmt} | side={side_fmt} | "
                f"type={type_fmt} | qty={quantity_precise} | price={price_precise} | "
                f"params={processed_params}"
            )

            # 从 processed_params 中拿出 take_profit / stop_loss 配置（不传给 Binance）
            take_profit = processed_params.get('take_profit')
            stop_loss = processed_params.get('stop_loss')
            # 保证不会被传进 api_params
            if 'take_profit' in order_params:
                del order_params['take_profit']
            if 'stop_loss' in order_params:
                del order_params['stop_loss']

            # ========== 3. 先创建主订单 ==========
            # 只保留 Binance 支持的字段
            standard_fields = [
                'symbol', 'side', 'type', 'quantity', 'price', 'timeInForce',
                'stopPrice', 'reduceOnly', 'positionSide', 'closePosition',
                'workingType', 'priceProtect',
                'newClientOrderId',

                # ✅ 原生跟踪委托参数
                'callbackRate',
                'activationPrice',
            ]
            api_params = {k: v for k, v in order_params.items() if k in standard_fields and v is not None}

            # ✅ 兼容 Binance 参数校验：部分触发单（STOP/TAKE_PROFIT 及其 LIMIT 变体）
            # 在某些环境/品种上不接受 reduceOnly（会报 -1106: Parameter 'reduceonly' sent when not required）
            # 触发限价止损/止盈本质上可由 side + positionSide + quantity 表达"平仓"，因此这里统一剔除。
            if api_params.get("type") in ["STOP", "TAKE_PROFIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]:
                api_params.pop("reduceOnly", None)

            # ✅ STOP（触发限价）必须同时具备 stopPrice + price + quantity（避免 silent fail / 参数不全）
            if api_params.get("type") == "STOP":
                if ("stopPrice" not in api_params) or ("price" not in api_params) or ("quantity" not in api_params):
                    raise ValueError(f"STOP(limit) 订单缺少必要参数: stopPrice/price/quantity, api_params={api_params}")

            # ✅ 原生跟踪委托 TRAILING_STOP_MARKET：清理不允许的字段，并检查 callbackRate
            if api_params.get("type") == "TRAILING_STOP_MARKET":
                # callbackRate: 0.1 ~ 5 (单位 %)，必填
                if "callbackRate" not in api_params or api_params["callbackRate"] is None:
                    raise ValueError("TRAILING_STOP_MARKET 需要 params['callbackRate']（例如 1 表示 1%）")

                # activationPrice 可选；不传则默认以市场价激活
                # trailing stop market 不需要 price / timeInForce / stopPrice
                api_params.pop("price", None)
                api_params.pop("timeInForce", None)
                api_params.pop("stopPrice", None)

                # workingType 可选；不传也行
                # api_params.setdefault("workingType", "CONTRACT_PRICE")

            # ====== ✅ 平全仓止损：用 STOP_MARKET + closePosition=True（不要走 Algo 接口） ======
            tag_u = str(processed_params.get("tag") or "").upper()
            is_sl_like = (("AUTO_" in tag_u or "MANUAL_" in tag_u) and ("SL" in tag_u or "STOP" in tag_u))

            if ("stopPrice" in api_params) and (api_params.get("type") in ["STOP", "STOP_MARKET"]) and is_sl_like:
                # 强制用 STOP_MARKET 做触发平仓
                api_params["type"] = "STOP_MARKET"

                # closePosition=True 表示“平该 positionSide 的全仓”
                api_params["closePosition"] = True

                # closePosition 模式下不要传这些字段
                api_params.pop("quantity", None)
                api_params.pop("price", None)
                api_params.pop("timeInForce", None)

                # 这两个也不要硬塞（避免触发币安参数校验问题）
                api_params.pop("reduceOnly", None)

                logger.info(
                    f"[BINANCE] CREATE_CLOSEPOS_SL | symbol={api_params.get('symbol')} | "
                    f"side={api_params.get('side')} | type={api_params.get('type')} | "
                    f"stopPrice={api_params.get('stopPrice')} | positionSide={api_params.get('positionSide')} | "
                    f"tag={processed_params.get('tag')}"
                )

                order = self.client.futures_create_order(**api_params)
            else:
                order = self.client.futures_create_order(**api_params)

            logger.info(f"币安API返回主订单: {order}")

            # 记录 TP/SL 子单 id
            order['takeProfitOrderId'] = None
            order['stopLossOrderId'] = None

            # ========== 4. 创建止盈订单（如果有配置） ==========
            if take_profit:
                tp_price = take_profit.get('price')
                if tp_price:
                    tp_price_precise = self.price_to_precision(symbol_fmt, tp_price)
                    logger.info(f"准备创建止盈订单，价格: {tp_price_precise}")

                    tp_side = 'SELL' if side_fmt == 'BUY' else 'BUY'

                    try:
                        logger.info(
                            f"[BINANCE] CREATE_TP_ORDER | symbol={symbol_fmt} | side={tp_side} | "
                            f"qty={quantity_precise} | price={tp_price_precise} | stop={tp_price_precise} | "
                            f"pos_side={order_params.get('positionSide')}"
                        )                       
                        tp_order = self.client.futures_create_order(
                            symbol=symbol_fmt,
                            side=tp_side,
                            type='TAKE_PROFIT',
                            quantity=quantity_precise,
                            price=tp_price_precise,
                            stopPrice=tp_price_precise,
                            timeInForce='GTC',
                            positionSide=order_params['positionSide'],
                            # 不传 reduceOnly，避免 -1106
                        )
                        logger.info(f"币安API返回止盈订单: {tp_order}")
                        order['takeProfitOrderId'] = tp_order.get('orderId')
                    except Exception as e:
                        logger.error(f"创建止盈订单失败: {e}")

            # ========== 5. 创建止损订单（如果有配置） ==========
            if stop_loss:
                sl_price = stop_loss.get('price')
                if sl_price:
                    sl_price_precise = self.price_to_precision(symbol_fmt, sl_price)
                    logger.info(f"准备创建止损订单，价格: {sl_price_precise}")

                    sl_side = 'SELL' if side_fmt == 'BUY' else 'BUY'

                    try:
                        logger.info(
                            f"[BINANCE] CREATE_SL_ORDER | symbol={symbol_fmt} | side={sl_side} | "
                            f"qty={quantity_precise} | price={sl_price_precise} | stop={sl_price_precise} | "
                            f"pos_side={order_params.get('positionSide')}"
                        )
                        sl_order = self.client.futures_create_order(
                            symbol=symbol_fmt,
                            side=sl_side,
                            type="STOP_MARKET",
                            stopPrice=sl_price_precise,
                            closePosition=True,
                            positionSide=order_params["positionSide"],
                        )

                        logger.info(f"币安API返回止损订单: {sl_order}")
                        order["stopLossOrderId"] = sl_order.get("orderId")

                    except Exception as e:
                        logger.error(f"创建止损订单失败: {e}")

            # ========== 6. 给主订单挂上 tag（方便上层识别 A1/A2） ==========
            if params and 'tag' in params:
                order['tag'] = params['tag']

            return order

        except Exception as e:
            logger.error(f"创建订单失败: {e}")
            # 只在调试模式下记录详细错误栈
            if logger.isEnabledFor(logging.DEBUG):
                import traceback
                logger.error(f"错误栈: {traceback.format_exc()}")
            # 只有在 dry_run 模式下才返回模拟数据，否则抛出异常
            if self.dry_run:
                symbol_fmt = self._format_symbol(symbol)
                side_fmt = self._format_side(side)
                type_fmt = self._format_order_type(order_type)

                order = {
                    'orderId': str(int(time.time() * 1000)),
                    'symbol': symbol_fmt,
                    'status': 'NEW',
                    'clientOrderId': f'dry_{int(time.time() * 1000)}',
                    'price': str(price or 0.0),
                    'avgPrice': '0.0',
                    'origQty': '0.01',
                    'executedQty': '0.0',
                    'cumQuote': '0.0',
                    'timeInForce': 'GTC',
                    'type': type_fmt,
                    'reduceOnly': False,
                    'closePosition': False,
                    'side': side_fmt,
                    'positionSide': 'BOTH',
                    'stopPrice': '0.0',
                    'workingType': 'CONTRACT_PRICE',
                    'priceProtect': False,
                    'origType': type_fmt,
                    'time': int(time.time() * 1000),
                    'updateTime': int(time.time() * 1000)
                }
                if params and 'tag' in params:
                    order['tag'] = params['tag']
                return order
            else:
                # 在非 dry_run 模式下，抛出异常，让上层处理（比如重试主单）
                raise

    
    def get_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """
        获取订单信息
        
        Args:
            order_id: 订单ID
            symbol: 交易对 (可选)
            
        Returns:
            订单信息字典
        """
        try:
            if self.dry_run:
                # 模拟获取订单信息
                symbol = self._format_symbol(symbol) if symbol else 'BTCUSDT'
                
                return {
                    'orderId': order_id,
                    'symbol': symbol,
                    'status': 'NEW',
                    'clientOrderId': f'dry_{order_id}',
                    'price': '45000.0',
                    'avgPrice': '0.0',
                    'origQty': '0.01',
                    'executedQty': '0.0',
                    'cumQuote': '0.0',
                    'timeInForce': 'GTC',
                    'type': 'LIMIT',
                    'reduceOnly': False,
                    'closePosition': False,
                    'side': 'BUY',
                    'positionSide': 'BOTH',
                    'stopPrice': '0.0',
                    'workingType': 'CONTRACT_PRICE',
                    'priceProtect': False,
                    'origType': 'LIMIT',
                    'time': 1672531200000,
                    'updateTime': 1672531200000
                }
            
            symbol = self._format_symbol(symbol) if symbol else None
            
            # 使用python-binance库获取订单信息
            order = self.client.futures_get_order(
                symbol=symbol,
                orderId=order_id
            )
            return order
        except Exception as e:
            logger.error(f"获取订单失败: {e}")
            # 返回模拟数据作为后备
            symbol = self._format_symbol(symbol) if symbol else 'BTCUSDT'
            
            return {
                'orderId': order_id,
                'symbol': symbol,
                'status': 'NEW',
                'clientOrderId': f'dry_{order_id}',
                'price': '45000.0',
                'avgPrice': '0.0',
                'origQty': '0.01',
                'executedQty': '0.0',
                'cumQuote': '0.0',
                'timeInForce': 'GTC',
                'type': 'LIMIT',
                'reduceOnly': False,
                'closePosition': False,
                'side': 'BUY',
                'positionSide': 'BOTH',
                'stopPrice': '0.0',
                'workingType': 'CONTRACT_PRICE',
                'priceProtect': False,
                'origType': 'LIMIT',
                'time': 1672531200000,
                'updateTime': 1672531200000
            }
    
    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单ID（可以是数字 orderId 或字符串 clientOrderId）
            symbol: 交易对 (可选)
            
        Returns:
            是否取消成功
        """
        try:
            if self.dry_run:
                # 模拟取消订单
                logger.info(f"[DRY RUN] 取消订单成功: {order_id}")
                return True
            
            if not symbol:
                raise Exception("取消订单必须提供交易对")
            
            symbol = self._format_symbol(symbol)
            
            # order_id 可能是纯数字的 orderId，也可能是字符串 clientOrderId
            try:
                # 优先按数字 orderId 撤单
                oid = int(order_id)
                logger.info(f"尝试按 orderId 撤单: symbol={symbol}, orderId={oid}")
                result = self.client.futures_cancel_order(
                    symbol=symbol,
                    orderId=oid
                )
            except (ValueError, TypeError):
                # 如果不能转成 int，则按 clientOrderId 撤单
                logger.info(
                    f"order_id 不是纯数字，按 origClientOrderId 撤单: "
                    f"symbol={symbol}, origClientOrderId={order_id}"
                )
                result = self.client.futures_cancel_order(
                    symbol=symbol,
                    origClientOrderId=str(order_id)
                )
            
            logger.info(f"取消订单返回结果: {result}")
            return True if result else False

        except Exception as e:
            logger.error(f"取消订单失败: {e}")
            return False
    
    def cancel_all_orders(self, symbol: Optional[str] = None, side: Optional[str] = None) -> bool:
        """
        取消所有订单
        
        Args:
            symbol: 交易对 (可选，默认取消所有)
            side: 订单方向 (可选，默认取消所有方向)
            
        Returns:
            是否取消成功
        """
        try:
            if self.dry_run:
                # 模拟取消所有订单
                symbol_str = symbol if symbol else "所有交易对"
                side_str = side if side else "所有方向"
                logger.info(f"[DRY RUN] 取消{symbol_str} {side_str}所有订单成功")
                return True
            
            if not symbol:
                raise Exception("取消所有订单必须提供交易对")
            
            symbol = self._format_symbol(symbol)
            
            if side:
                # 如果指定了方向，先获取所有订单，再过滤取消
                orders = self.get_open_orders(symbol)
                if not orders:
                    logger.warning("⚠️ 获取未成交订单失败或为空，无法按方向逐个取消")
                    return False
                success_count = 0
                for order in orders:
                    if order['side'] == self._format_side(side):
                        if self.cancel_order(order['orderId'], symbol):
                            success_count += 1
                logger.info(f"取消{symbol} {side}方向订单成功: {success_count}/{len(orders)}个订单")
                return success_count > 0
            else:
                # 如果没有指定方向，直接取消所有订单
                result = self.client.futures_cancel_all_open_orders(symbol=symbol)
                logger.info(f"取消所有订单成功: {result}")
                return True
        except Exception as e:
            logger.error(f"取消所有订单失败: {e}")
            return False
    
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取所有未成交订单
        
        Args:
            symbol: 交易对 (可选，默认获取所有)
            
        Returns:
            未成交订单列表；如果请求失败或处于冷却期，返回 []
        """
        # ⭐ 0. WS 已就绪：直接返回 WS 缓存（优先于冷却/REST）
        if not self.dry_run and getattr(self, "_ws_account_ready", False):
            return self._get_ws_open_orders(symbol)

        # ⭐ 1. 冷却期内直接跳过真实请求
        if self._is_in_rate_limit_cooldown():
            return []

        try:
            if self.dry_run:
                # 模拟未成交订单
                logger.info(f"[DRY RUN] 获取未成交订单: {symbol}")
                return []  # 模拟没有未成交订单
            
            if not symbol:
                raise Exception("获取未成交订单必须提供交易对")
            
            symbol = self._format_symbol(symbol)
            
            # 使用python-binance库获取未成交订单
            orders = self.client.futures_get_open_orders(symbol=symbol)

            # ⭐ 调用成功，说明当前没有被限流，重置 -1003 计数
            self.consecutive_1003 = 0
            return orders

        except Exception as e:
            # ⭐ 交给限流处理函数判断是否为 -1003，并决定是否进入冷却期
            self._handle_rate_limit_error(e, context="get_open_orders")
            logger.error(f"获取未成交订单失败: {e}", exc_info=True)
            return []
    
    def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取持仓信息
        
        Args:
            symbol: 交易对 (可选，默认返回所有)
            
        Returns:
            持仓信息列表
        """
        # key: 用于缓存的索引；symbol=None 时用 "__ALL__"
        cache_key = "__ALL__"
        symbol_fmt: Optional[str] = None
        if symbol:
            symbol_fmt = self._format_symbol(symbol)
            cache_key = symbol_fmt

        # ⭐ 0. WS 已就绪：优先从 WS positions 缓存读取（避免 REST 轮询）
        if not self.dry_run and getattr(self, "_ws_account_ready", False):
            if symbol_fmt:
                p = self._ws_positions.get(symbol_fmt)
                result = [p] if p else []
            else:
                result = list(self._ws_positions.values())

            self._last_positions[cache_key] = result
            return result

        # ⭐ 1. 冷却期内优先返回缓存的真实持仓
        if self._is_in_rate_limit_cooldown():
            cached = self._last_positions.get(cache_key)
            if cached is not None:
                logger.warning(f"[RATE LIMIT] 冷却期内 get_positions({cache_key}) 使用缓存持仓数据")
                return cached

            # 没有缓存，只能返回一份“空仓”的模拟数据
            logger.warning(f"[RATE LIMIT] 冷却期内 get_positions({cache_key}) 无缓存，返回模拟空仓数据")
            positions: List[Dict] = []
            symbol_for_mock = symbol_fmt or 'BTCUSDT'
            positions.append({
                'symbol': symbol_for_mock,
                'positionAmt': '0.0',
                'entryPrice': '0.0',
                'markPrice': '45000.0',
                'unRealizedProfit': '0.0',
                'liquidationPrice': '0.0',
                'leverage': '10',
                'maxNotionalValue': '25000000.0',
                'marginType': 'cross',
                'isolatedMargin': '0.0',
                'isAutoAddMargin': 'false',
                'positionSide': 'BOTH',
                'notional': '0.0',
                'isolatedWallet': '0.0',
                'updateTime': 1672531200000,
            })
            return positions

        try:
            if self.dry_run:
                # 模拟持仓信息（测试模式）
                positions: List[Dict] = []
                symbol_for_mock = symbol_fmt or 'BTCUSDT'
                positions.append({
                    'symbol': symbol_for_mock,
                    'positionAmt': '0.0',
                    'entryPrice': '0.0',
                    'markPrice': '45000.0',
                    'unRealizedProfit': '0.0',
                    'liquidationPrice': '0.0',
                    'leverage': '10',
                    'maxNotionalValue': '25000000.0',
                    'marginType': 'cross',
                    'isolatedMargin': '0.0',
                    'isAutoAddMargin': 'false',
                    'positionSide': 'BOTH',
                    'notional': '0.0',
                    'isolatedWallet': '0.0',
                    'updateTime': 1672531200000,
                })
                return positions
            
            logger.info(f"尝试获取持仓信息: symbol={symbol}, proxy={self.proxy}")
            # 使用python-binance库获取持仓信息（所有 symbol）
            positions_all = self.client.futures_position_information()
            logger.info(f"获取持仓信息成功: {positions_all}")

            # ⭐ 调用成功：重置连续 -1003 计数
            self.consecutive_1003 = 0

            # 根据 symbol 过滤
            if symbol_fmt:
                positions = [p for p in positions_all if p.get('symbol') == symbol_fmt]
            else:
                positions = positions_all

            # ⭐ 更新缓存
            self._last_positions[cache_key] = positions

            return positions

        except Exception as e:
            # ⭐ 出错：交给限流逻辑处理 -1003
            self._handle_rate_limit_error(e, context="get_positions")

            logger.error(f"获取持仓信息失败: {type(e).__name__}: {e}", exc_info=True)

            # ⭐ 优先返回缓存的真实数据
            cached = self._last_positions.get(cache_key)
            if cached is not None:
                logger.warning(f"获取持仓失败，get_positions({cache_key}) 返回缓存持仓数据")
                return cached

            # 没有缓存，就返回“空仓”模拟数据兜底
            logger.warning(f"获取持仓失败且无缓存，get_positions({cache_key}) 返回模拟空仓数据")
            positions: List[Dict] = []
            symbol_for_mock = symbol_fmt or 'BTCUSDT'
            positions.append({
                'symbol': symbol_for_mock,
                'positionAmt': '0.0',
                'entryPrice': '0.0',
                'markPrice': '45000.0',
                'unRealizedProfit': '0.0',
                'liquidationPrice': '0.0',
                'leverage': '10',
                'maxNotionalValue': '25000000.0',
                'marginType': 'cross',
                'isolatedMargin': '0.0',
                'isAutoAddMargin': 'false',
                'positionSide': 'BOTH',
                'notional': '0.0',
                'isolatedWallet': '0.0',
                'updateTime': 1672531200000,
            })
            return positions
    
    def get_kline(self, symbol: str, interval: str, limit: int = 100) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            symbol: 交易对
            interval: 时间周期
            limit: 获取数量
            
        Returns:
            K线数据列表
        """
        try:
            if self.dry_run:
                # 模拟K线数据
                symbol = self._format_symbol(symbol)
                formatted_klines = []
                base_price = 45000.0
                current_time = 1672531200000  # 起始时间
                
                # 根据时间周期计算时间间隔
                interval_ms = {
                    '1m': 60000,
                    '5m': 300000,
                    '15m': 900000,
                    '30m': 1800000,
                    '1h': 3600000,
                    '4h': 14400000,
                    '1d': 86400000
                }.get(interval, 3600000)  # 默认1小时
                
                for i in range(limit):
                    # 生成随机波动的K线数据
                    open_price = base_price + (i % 20 - 10) * 100
                    high_price = open_price + 200
                    low_price = open_price - 200
                    close_price = open_price + (i % 10 - 5) * 50
                    
                    formatted_klines.append({
                        'timestamp': current_time + i * interval_ms,
                        'open': open_price,
                        'high': high_price,
                        'low': low_price,
                        'close': close_price,
                        'volume': 1000 + i * 100
                    })
                
                return formatted_klines
            
            symbol = self._format_symbol(symbol)
            # 使用python-binance库获取K线数据
            klines = self.client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            # 格式化K线数据
            formatted_klines = []
            for kline in klines:
                formatted_klines.append({
                    'timestamp': kline[0],
                    'open': float(kline[1]),
                    'high': float(kline[2]),
                    'low': float(kline[3]),
                    'close': float(kline[4]),
                    'volume': float(kline[5])
                })
            
            return formatted_klines
        except Exception as e:
            logger.error(f"获取K线数据失败: {e}")
            # 返回模拟数据作为后备
            formatted_klines = []
            base_price = 45000.0
            current_time = 1672531200000
            
            for i in range(min(limit, 100)):
                formatted_klines.append({
                    'timestamp': current_time + i * 3600000,
                    'open': base_price + (i % 20 - 10) * 100,
                    'high': base_price + (i % 20 - 10) * 100 + 200,
                    'low': base_price + (i % 20 - 10) * 100 - 200,
                    'close': base_price + (i % 20 - 10) * 100 + (i % 10 - 5) * 50,
                    'volume': 1000 + i * 100
                })
            
            return formatted_klines
    
    def _format_symbol(self, symbol: str) -> str:
        """
        格式化交易对
        
        Args:
            symbol: 交易对 (如 BTC/USDT)
            
        Returns:
            格式化后的交易对 (如 BTCUSDT)
        """
        return symbol.replace('/', '')
    
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

    
    def price_to_precision(self, symbol: str, price: float) -> float:
        """
        将价格格式化为交易所支持的精度
        
        Args:
            symbol: 交易对
            price: 价格
            
        Returns:
            格式化后的价格
        """
        try:
            formatted_symbol = self._format_symbol(symbol)
            
            # 优先从交易所获取实际精度
            if not self.dry_run:
                try:
                    # 获取交易对信息
                    exchange_info = self.client.futures_exchange_info()
                    for s in exchange_info['symbols']:
                        if s['symbol'] == formatted_symbol:
                            # 找到交易对，获取价格精度
                            price_precision = s['pricePrecision']
                            logger.debug(f"从交易所获取到{formatted_symbol}的价格精度: {price_precision}")
                            # 使用字符串格式化确保精确的小数位数
                            formatted_price = round(price, price_precision)
                            # 转换为float返回，确保格式正确
                            return formatted_price
                except Exception as e:
                    logger.warning(f"从交易所获取价格精度失败，使用默认值: {e}")
            
            # 硬编码的精度映射（作为备选）
            precision_map = {
                'BTCUSDT': 2,
                'BTCUSDC': 2,
                'ETHUSDT': 2,
                'ETHUSDC': 2,
                'BNBUSDT': 3,
                'BNBUSDC': 3,
                'SOLUSDT': 2,
                'SOLUSDC': 2,
                'ADAUSDT': 4,
                'ADAUSDC': 4,
                'XRPUSDT': 4,
                'XRPUSDC': 4
            }
            
            # 获取精度，默认2位小数（适合大多数主流交易对）
            precision = precision_map.get(formatted_symbol, 2)
            logger.debug(f"使用默认价格精度{precision}处理{formatted_symbol}的价格")
            # 使用字符串格式化确保精确的小数位数
            formatted_price = round(price, precision)
            return formatted_price
        except Exception as e:
            logger.error(f"价格精度处理失败: {e}")
            # 回退方案，保留2位小数
            return round(price, 2)
    
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """
        将数量格式化为交易所支持的精度
        
        Args:
            symbol: 交易对
            amount: 数量
            
        Returns:
            格式化后的数量
        """
        try:
            formatted_symbol = self._format_symbol(symbol)
            
            # 优先从交易所获取实际精度
            if not self.dry_run:
                try:
                    # 获取交易对信息
                    exchange_info = self.client.futures_exchange_info()
                    for s in exchange_info['symbols']:
                        if s['symbol'] == formatted_symbol:
                            # 找到交易对，获取数量精度
                            amount_precision = s['quantityPrecision']
                            logger.debug(f"从交易所获取到{formatted_symbol}的数量精度: {amount_precision}")
                            # 使用字符串格式化确保精确的小数位数
                            formatted_amount = round(amount, amount_precision)
                            return formatted_amount
                except Exception as e:
                    logger.warning(f"从交易所获取数量精度失败，使用默认值: {e}")
            
            # 硬编码的精度映射（作为备选）
            precision_map = {
                'BTCUSDT': 3,
                'BTCUSDC': 3,
                'ETHUSDT': 3,
                'ETHUSDC': 3,
                'BNBUSDT': 2,
                'BNBUSDC': 2,
                'SOLUSDT': 2,
                'SOLUSDC': 2,
                'ADAUSDT': 0,
                'ADAUSDC': 0,
                'XRPUSDT': 0,
                'XRPUSDC': 0
            }
            
            # 获取精度，默认2位小数
            precision = precision_map.get(formatted_symbol, 2)
            logger.debug(f"使用默认数量精度{precision}处理{formatted_symbol}的数量")
            # 使用字符串格式化确保精确的小数位数
            formatted_amount = round(amount, precision)
            return formatted_amount
        except Exception as e:
            logger.error(f"数量精度处理失败: {e}")
            # 回退方案，保留2位小数
            return round(amount, 2)
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        设置杠杆倍数

        Args:
            symbol: 交易对
            leverage: 杠杆倍数

        Returns:
            是否设置成功
        """
        try:
            if self.dry_run:
                logger.info(f"模拟设置杠杆: {symbol} {leverage}x")
                return True

            # ✅ 冷却期：不要再打币安
            if self._is_in_rate_limit_cooldown():
                logger.warning(f"[RATE LIMIT] 冷却期内跳过 set_leverage({symbol}, {leverage})")
                return False

            formatted_symbol = self._format_symbol(symbol)
            logger.info(f"设置杠杆: {symbol} -> {leverage}x")

            result = self.client.futures_change_leverage(
                symbol=formatted_symbol,
                leverage=leverage
            )
            logger.info(f"设置杠杆成功: {result}")

            # ✅ 成功后写入去重缓存
            self._leverage_cache[formatted_symbol] = int(leverage)

            self.consecutive_1003 = 0
            return True

        except Exception as e:
            self._handle_rate_limit_error(e, context="set_leverage")
            logger.error(f"设置杠杆失败: {e}", exc_info=True)
            return False


    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        """设置保证金模式（逐仓/全仓）。

        Args:
            symbol: 交易对，如 "ETHUSDT"
            margin_type: "ISOLATED" 或 "CROSSED"/"CROSS"

        Returns:
            是否设置成功（若已是目标模式，也返回 True）
        """
        try:
            if self.dry_run:
                logger.info(f"模拟设置保证金模式: {symbol} -> {margin_type}")
                return True

            formatted_symbol = self._format_symbol(symbol)
            mt = (margin_type or "").upper()
            if mt in ("CROSS", "CROSSED"):
                mt = "CROSSED"
            elif mt not in ("ISOLATED", "CROSSED"):
                logger.warning(f"未知 margin_type={margin_type}，将按 ISOLATED 处理")
                mt = "ISOLATED"

            logger.info(f"设置保证金模式: {formatted_symbol} -> {mt}")
            result = self.client.futures_change_margin_type(
                symbol=formatted_symbol,
                marginType=mt
            )
            logger.info(f"设置保证金模式成功: {result}")
            return True

        except BinanceAPIException as e:
            # -4046: No need to change margin type.
            if getattr(e, "code", None) == -4046:
                logger.info(f"保证金模式无需变更(已是目标模式): {symbol} -> {margin_type}")
                return True
            logger.error(f"设置保证金模式失败: {e}")
            return False
        except Exception as e:
            logger.error(f"设置保证金模式失败: {e}")
            return False

    
    # =========================
    # ⭐ 动态交易对筛选（USDT 永续）：Top 幅度 + 反转价值过滤（多周期 + 回撤比例）
    # =========================
    def get_top_reversal_pairs_usdt(
        self,
        top_n: int = 3,
        min_abs_pct: float = 50.0,
        fallback_top1_if_none: bool = True,
        # 位置过滤：做空需贴近高位；做多需贴近低位
        pos_threshold_short: float = 0.75,
        pos_threshold_long: float = 0.25,
        # 回撤/反弹过滤（用于避开“先大涨后大跌 / 先大跌后大涨”已走完的行情）
        max_retrace_ratio: float = 0.30,
        # 多周期：72h 用 1h K线；10天用 1d K线(10根)
        kline_72h_interval: str = "1h",
        kline_72h_bars: int = 72,
        kline_10d_interval: str = "1d",
        kline_10d_bars: int = 10,
        # 性能/限流：只对候选集拉 K 线
        preselect_limit: int = 30,
        cache_ttl: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        选出“涨多做空 / 跌多做多”的候选交易对（USDT 永续）：
        1) 先用 futures_ticker 的 24h 涨跌幅筛：abs(pct) >= min_abs_pct
        2) 再用 72h(1h K线) + 10天(1d K线) 做“位置(pos) + 回撤/反弹比例”过滤：
           - 做空(pct>0)：需同时满足 pos72、pos24(取72h最后24根) 与 pos10d 都贴近高位，
             且从各周期高位回撤比例 (high-last)/high 不超过 max_retrace_ratio
           - 做多(pct<0)：需同时满足 pos72、pos24 与 pos10d 都贴近低位，
             且从各周期低位反弹比例 (last-low)/low 不超过 max_retrace_ratio
        返回：按 abs_pct 降序的 dict 列表，每项包含 symbol/pct/mode 等字段。
        """
        now = time.time()

        # cache（避免频繁打 REST）
        cache_key = (
            f"toprev_usdt_{top_n}_{min_abs_pct}_{pos_threshold_short}_{pos_threshold_long}_"
            f"{max_retrace_ratio}_{kline_72h_interval}_{kline_72h_bars}_{kline_10d_interval}_{kline_10d_bars}_{preselect_limit}"
        )
        if not hasattr(self, "_top_reversal_cache"):
            self._top_reversal_cache = {}
        cached = self._top_reversal_cache.get(cache_key)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) <= max(1, int(cache_ttl))):
            return cached.get("data") or []

        # 冷却期：不要打 REST（返回缓存或空）
        try:
            if self._is_in_rate_limit_cooldown():
                return cached.get("data") if cached else []
        except Exception:
            pass

        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        def _pos(last: float, lo: float, hi: float) -> float:
            if hi <= lo:
                return 0.5
            return (last - lo) / (hi - lo)

        def _window_stats(klines: List[Dict[str, Any]]):
            if not klines:
                return None
            lo = min(float(k.get("low", 0.0) or 0.0) for k in klines)
            hi = max(float(k.get("high", 0.0) or 0.0) for k in klines)
            last = float(klines[-1].get("close", 0.0) or 0.0)
            return lo, hi, last

        # 1) 取全市场 24h ticker（USDT 永续）
        try:
            tickers = self.client.futures_ticker() if not self.dry_run else []
        except Exception as e:
            try:
                self._handle_rate_limit_error(e, context="get_top_reversal_pairs_usdt:futures_ticker")
            except Exception:
                pass
            return []

        prelim: List[Dict[str, Any]] = []
        all_usdt: List[Dict[str, Any]] = []

        for t in (tickers or []):
            sym = str(t.get("symbol") or "")
            if not sym.endswith("USDT"):
                continue

            pct = _sf(t.get("priceChangePercent"))
            abs_pct = abs(pct)

            # 记录所有（用于兜底 top1）
            all_usdt.append({"symbol": sym, "pct": pct, "abs_pct": abs_pct})

            if abs_pct < float(min_abs_pct):
                continue

            mode = "short" if pct >= 0 else "long"
            prelim.append({"symbol": sym, "pct": pct, "abs_pct": abs_pct, "mode": mode})

        # 2) 没有 >= min_abs_pct 的候选：兜底 top1（不做形态过滤）
        if not prelim and fallback_top1_if_none:
            all_usdt.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
            if all_usdt:
                prelim = [dict(all_usdt[0])]
                prelim[0]["mode"] = "short" if float(prelim[0].get("pct") or 0.0) >= 0 else "long"
                prelim[0]["fallback"] = True

        prelim.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
        prelim = prelim[: max(1, int(preselect_limit))]

        results: List[Dict[str, Any]] = []

        for c in prelim:
            sym = c["symbol"]
            mode = c["mode"]

            # --- 72h: 1h K线（limit=72） ---
            kl72 = self.get_kline(sym, interval=kline_72h_interval, limit=int(kline_72h_bars)) or []
            if not kl72:
                if c.get("fallback"):
                    results.append(c)
                continue

            stats72 = _window_stats(kl72)
            if not stats72:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo72, hi72, last = stats72

            # 24h 区间：直接取 72h K线的最后 24 根（避免只看 ticker 的 24h 高低价导致跨24h尖峰丢失）
            kl24 = kl72[-24:] if len(kl72) >= 24 else kl72
            stats24 = _window_stats(kl24) if kl24 else None
            if not stats24:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo24, hi24, last24 = stats24

            # --- 10天: 1d K线10根 ---
            kl10d = self.get_kline(sym, interval=kline_10d_interval, limit=int(kline_10d_bars)) or []
            if not kl10d:
                if c.get("fallback"):
                    results.append(c)
                continue
            stats10 = _window_stats(kl10d)
            if not stats10:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo10, hi10, last10 = stats10

            # 用最新 close（1h）为 last；日线 last 仅用于统计窗口
            pos72 = _pos(last, lo72, hi72)
            pos24 = _pos(last, lo24, hi24)
            pos10 = _pos(last, lo10, hi10)

            # 回撤/反弹比例
            pullback72 = (hi72 - last) / hi72 if hi72 > 0 else 0.0
            pullback24 = (hi24 - last) / hi24 if hi24 > 0 else 0.0
            pullback10 = (hi10 - last) / hi10 if hi10 > 0 else 0.0

            rebound72 = (last - lo72) / lo72 if lo72 > 0 else 0.0
            rebound24 = (last - lo24) / lo24 if lo24 > 0 else 0.0
            rebound10 = (last - lo10) / lo10 if lo10 > 0 else 0.0

            ok = True
            if mode == "short":
                # 仍贴近高位 + 回撤不能太大
                if (pos72 < float(pos_threshold_short)) or (pos24 < float(pos_threshold_short)) or (pos10 < float(pos_threshold_short)):
                    ok = False
                if (pullback72 > float(max_retrace_ratio)) or (pullback24 > float(max_retrace_ratio)) or (pullback10 > float(max_retrace_ratio)):
                    ok = False
            else:
                # 仍贴近低位 + 反弹不能太大
                if (pos72 > float(pos_threshold_long)) or (pos24 > float(pos_threshold_long)) or (pos10 > float(pos_threshold_long)):
                    ok = False
                if (rebound72 > float(max_retrace_ratio)) or (rebound24 > float(max_retrace_ratio)) or (rebound10 > float(max_retrace_ratio)):
                    ok = False

            if not ok:
                continue

            c.update({
                "pos_72h": pos72,
                "pos_24h": pos24,
                "pos_10d": pos10,
                "pullback_72h": pullback72,
                "pullback_24h": pullback24,
                "pullback_10d": pullback10,
                "rebound_72h": rebound72,
                "rebound_24h": rebound24,
                "rebound_10d": rebound10,
                "last_kline_1h": last,
                "low_72h": lo72, "high_72h": hi72,
                "low_10d": lo10, "high_10d": hi10,
            })
            results.append(c)

        results.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
        results = results[: max(1, int(top_n))]

        self._top_reversal_cache[cache_key] = {"ts": now, "data": results}
        return results


def list_tradeable_contracts(
        self,
        quote_asset: str = "USDT",
        contract_type: str = "PERPETUAL",
        status: str = "TRADING"
    ) -> List[str]:
        """列出可交易合约列表（USDT 合约为主）。

        通过 Binance Futures exchangeInfo 过滤：
        - quoteAsset == quote_asset（默认 USDT）
        - contractType == contract_type（默认 PERPETUAL）
        - status == status（默认 TRADING）

        Returns:
            符号列表，例如 ["BTCUSDT", "ETHUSDT", ...]
        """
        try:
            if self.dry_run:
                # dry_run 下返回一个最小可用集合
                return ["BTCUSDT", "ETHUSDT"]

            qa = (quote_asset or "USDT").upper()
            ct = (contract_type or "PERPETUAL").upper()
            st = (status or "TRADING").upper()

            info = self.client.futures_exchange_info()
            symbols: List[str] = []
            for s in (info.get("symbols") or []):
                try:
                    if st and (s.get("status") or "").upper() != st:
                        continue
                    if qa and (s.get("quoteAsset") or "").upper() != qa:
                        continue
                    if ct and (s.get("contractType") or "").upper() != ct:
                        continue
                    sym = s.get("symbol")
                    if sym:
                        symbols.append(sym)
                except Exception:
                    continue

            # 去重 + 排序，方便稳定输出
            symbols = sorted(list(dict.fromkeys(symbols)))
            logger.info(f"可交易合约数量: {len(symbols)} | quote={qa} contractType={ct} status={st}")
            return symbols

        except Exception as e:
            logger.error(f"获取可交易合约列表失败: {e}", exc_info=True)
            return []