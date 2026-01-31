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


class AccountMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ✅ 最近一次成功获取的“全量持仓”
        self._last_positions_all_ok = None

        self._last_positions = {}
        self._last_positions_ts = {}

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
        
        # ⭐ 1.5) user stream 失败降级：限制 balance REST 频率，优先返回缓存
        if self._should_throttle_account_rest("balance"):
            if self._last_balance is not None:
                logger.warning("[DEGRADED] get_balance throttled, return cached balance")
                return self._last_balance
            return {currency: {"free": 0.0, "used": 0.0, "total": 0.0}, "totalWalletBalance": 0.0}

        # ⭐ 检查客户端是否初始化
        if self.client is None:
            logger.warning("client is None，跳过私有接口调用（初始化失败）")
            if self._last_balance is not None:
                return self._last_balance
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
        now = time.time()

        if symbol:
            symbol_fmt = self._format_symbol(symbol)
            cache_key = symbol_fmt

        # ✅ TTL 缓存：避免 UI/策略反复打 positionRisk
        ttl = float(
            (getattr(self, "global_config", {}) or {}).get("positions_cache_ttl_sec", 10)
        )

        # 1) symbol 查询：优先从 __ALL__ 缓存过滤
        if symbol_fmt:
            all_cached = self._last_positions.get("__ALL__")
            all_ts = self._last_positions_ts.get("__ALL__", 0)
            if all_cached and (now - all_ts) <= ttl:
                return [p for p in all_cached if p.get("symbol") == symbol_fmt]

        # 2) 全量查询：缓存足够新鲜直接返回
        if cache_key == "__ALL__":
            cached = self._last_positions.get("__ALL__")
            ts = self._last_positions_ts.get("__ALL__", 0)
            if cached and (now - ts) <= ttl:
                return cached

        # ⭐ 0. WS 已就绪：优先从 WS positions 缓存读取（避免 REST 轮询）
        if not self.dry_run and getattr(self, "_ws_account_ready", False):
            if symbol_fmt:
                p = self._ws_positions.get(symbol_fmt)
                result = [p] if p else []
            else:
                result = list(self._ws_positions.values())

            # ✅ 缓存全量持仓（用于节流时兜底）
            self._last_positions_all_ok = list(self._ws_positions.values())
            self._last_positions[cache_key] = result
            self._last_positions_ts[cache_key] = time.time()
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
        
        # ⭐ 1.5) user stream 失败降级：限制 positions REST 频率，优先返回缓存
        if self._should_throttle_account_rest("positions"):
            cached = self._last_positions.get(cache_key)
            if cached is not None:
                logger.warning(f"[DEGRADED] get_positions({cache_key}) throttled, return cached")
                return cached
            # ✅ 关键修复：节流时绝不返回 []（会把 UI 清空）
            last_ok = getattr(self, "_last_positions_all_ok", None)
            if last_ok:
                logger.warning(
                    f"[DEGRADED] get_positions({cache_key}) throttled, no cache -> return last_ok_all"
                )
                if symbol:
                    symbol_fmt = self._format_symbol(symbol)
                    return [p for p in last_ok if p.get("symbol") == symbol_fmt]
                return last_ok

            logger.warning(
                f"[DEGRADED] get_positions({cache_key}) throttled, no cache/last_ok -> return []"
            )
            return []

        # ⭐ 检查客户端是否初始化
        if self.client is None:
            logger.warning("client is None，跳过私有接口调用（初始化失败）")
            cached = self._last_positions.get(cache_key)
            if cached is not None:
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

            # ✅ 缓存全量持仓（用于节流时兜底）
            self._last_positions_all_ok = positions_all
            self._last_positions["__ALL__"] = positions_all
            self._last_positions_ts["__ALL__"] = now

            # 根据 symbol 过滤
            if symbol_fmt:
                positions = [p for p in positions_all if p.get('symbol') == symbol_fmt]
            else:
                positions = positions_all

            # ⭐ 更新缓存
            self._last_positions[cache_key] = positions
            self._last_positions_ts[cache_key] = time.time()

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

    def get_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """
        获取订单信息
        
        Args:
            order_id: 订单ID
            symbol: 交易对 (可选)
            
        Returns:
            订单信息字典
        """
        # ⭐ 检查客户端是否初始化
        if self.client is None:
            logger.warning("client is None，跳过私有接口调用（初始化失败）")
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
        
        # ⭐ 1.5) user stream 失败降级：限制 open_orders REST 频率
        if self._should_throttle_account_rest("open_orders"):
            logger.warning("[DEGRADED] get_open_orders throttled, return []")
            return []

        # ⭐ 检查客户端是否初始化
        if self.client is None:
            logger.warning("client is None，跳过私有接口调用（初始化失败）")
            return []

        try:
            if self.dry_run:
                # 模拟未成交订单
                logger.info(f"[DRY RUN] 获取未成交订单: {symbol}")
                return []  # 模拟没有未成交订单
            
            if not symbol:
                # WS 未就绪时 Binance REST 端无法“全量获取未成交”，这里不要抛异常，直接返回空列表
                # 账户页仍能正常渲染；一旦 WS ready，会走函数最上面的 WS 分支返回全量缓存
                logger.warning("get_open_orders: symbol=None 且 WS 未就绪，无法获取全量未成交订单，返回 []")
                return []

            
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
