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


class MarketDataMixin:
    def __init__(self, *args, **kwargs):
        self._last_ticker_fetch_ts: dict[str, float] = {}
        self._last_ticker_cache: dict[str, dict] = {}

    def fetch_ticker(self, symbol: str):
        """
        从REST API获取行情信息
        
        Args:
            symbol: 交易对
            
        Returns:
            行情信息字典
        """
        symbol_fmt = self._format_symbol(symbol)

        # ⭐ 1. 如果当前处于限流冷却期，优先返回缓存的真实行情
        if self._is_in_rate_limit_cooldown():
            cached = self._last_ticker.get(symbol_fmt)
            if cached is not None:
                logger.warning(f"[RATE LIMIT] 冷却期内 fetch_ticker({symbol_fmt}) 使用缓存行情数据")
                return cached

            logger.error(
                   f"[RATE LIMIT] 冷却期内 fetch_ticker({symbol_fmt}) 无缓存，且不能返回模拟价，"
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
            self._handle_rate_limit_error(e, context="fetch_ticker")

            logger.error(f"获取行情信息失败: {e}")
            logger.error(f"错误类型: {type(e).__name__}")
            import traceback
            logger.error(f"错误栈: {traceback.format_exc()}")

            # ⭐ 优先返回缓存的真实数据
            cached = self._last_ticker.get(symbol_fmt)
            if cached is not None:
                logger.warning(f"获取行情失败，fetch_ticker({symbol_fmt}) 返回缓存行情数据")
                return cached

            # ✅ 改成：要么抛异常，要么返回明显的“无效价格”
            logger.error(f"获取行情失败且无缓存，fetch_ticker({symbol_fmt}) 无法返回有效行情")
            raise RuntimeError(f"无法获取 {symbol_fmt} 的真实行情")

    def get_ticker(self, symbol: str):
        sym = str(symbol).replace("/", "").upper()

        # 纯轮询：做一个最小间隔缓存，避免 1 秒内重复打 REST
        now = time.time()
        min_interval = float(getattr(self, "_rest_ticker_min_interval", 0.8))
        last_ts = float(self._last_ticker_fetch_ts.get(sym, 0.0))
        if (now - last_ts) < min_interval:
            cached = self._last_ticker_cache.get(sym)
            if cached:
                return cached

        t = self.fetch_ticker(symbol)  # 走 REST
        if isinstance(t, dict) and t:
            self._last_ticker_cache[sym] = t
            self._last_ticker_fetch_ts[sym] = now
        return t

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
