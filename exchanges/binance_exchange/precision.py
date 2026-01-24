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


class PrecisionMixin:
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

            # ✅ 首选：按 Binance filters 的 tickSize 对齐（解决 -4014 Price not increased by tick size）
            rules = self._get_symbol_rules(formatted_symbol)
            tick = rules.get("tickSize")
            if tick:
                aligned = self._align_to_step(price, tick)
                logger.debug(f"价格对齐 tickSize: {formatted_symbol} price={price} tick={tick} -> {aligned}")
                return aligned

            # fallback：拿不到 tickSize 时，退回 pricePrecision/硬编码（尽量兼容 dry_run 或异常场景）
            precision = 2
            if not self.dry_run:
                try:
                    exinfo = self._get_exchange_info_cached() or self.client.futures_exchange_info()
                    for s in exinfo.get("symbols", []):
                        if s.get("symbol") == formatted_symbol:
                            precision = int(s.get("pricePrecision", 2))
                            break
                except Exception as e:
                    logger.warning(f"获取 pricePrecision 失败，使用默认值: {e}")

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
            precision = precision_map.get(formatted_symbol, precision)
            formatted_price = round(price, precision)
            return formatted_price
        except Exception as e:
            logger.error(f"价格精度处理失败: {e}")
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

            # ✅ 首选：按 Binance filters 的 stepSize 对齐（LOT_SIZE/MARKET_LOT_SIZE）
            rules = self._get_symbol_rules(formatted_symbol)
            step = rules.get("stepSize")
            if step:
                aligned = self._align_to_step(amount, step)
                logger.debug(f"数量对齐 stepSize: {formatted_symbol} amount={amount} step={step} -> {aligned}")
                return aligned

            # fallback：拿不到 stepSize 时，退回 quantityPrecision/硬编码（尽量兼容 dry_run 或异常场景）
            precision = 2
            if not self.dry_run:
                try:
                    exinfo = self._get_exchange_info_cached() or self.client.futures_exchange_info()
                    for s in exinfo.get("symbols", []):
                        if s.get("symbol") == formatted_symbol:
                            precision = int(s.get("quantityPrecision", 2))
                            break
                except Exception as e:
                    logger.warning(f"获取 quantityPrecision 失败，使用默认值: {e}")

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
            precision = precision_map.get(formatted_symbol, precision)
            formatted_amount = round(amount, precision)
            return formatted_amount
        except Exception as e:
            logger.error(f"数量精度处理失败: {e}")
            return round(amount, 2)

    def _get_exchange_info_cached(self, ttl_sec: int = 300) -> Optional[Dict[str, Any]]:
        """获取 futures_exchange_info（带缓存）。

        - 避免每次精度处理都打 REST，降低 -1003 风险
        - 若处于限流冷却期，则返回 None
        """
        if self.dry_run:
            return None
        if self._is_in_rate_limit_cooldown():
            return None

        now = time.time()
        if self._exinfo_cache is not None and (now - float(self._exinfo_cache_ts)) <= ttl_sec:
            return self._exinfo_cache

        try:
            exinfo = self.client.futures_exchange_info()
            self._exinfo_cache = exinfo
            self._exinfo_cache_ts = now
            # 成功请求 -> 重置 -1003 连续计数
            self.consecutive_1003 = 0
            return exinfo
        except Exception as e:
            # 让全局限流逻辑接管（若是 -1003 会进入冷却）
            try:
                self._handle_rate_limit_error(e, context="futures_exchange_info")
            except Exception:
                pass
            logger.warning(f"获取 futures_exchange_info 失败(将使用 fallback 精度): {e}")
            return None

    def _get_symbol_rules(self, symbol: str) -> Dict[str, Optional[str]]:
        """返回该 symbol 的 tickSize/stepSize（来自 futures_exchange_info filters）。"""
        sym = self._format_symbol(symbol)
        cached = self._symbol_rule_cache.get(sym)
        if cached is not None:
            return cached

        rules: Dict[str, Optional[str]] = {"tickSize": None, "stepSize": None}
        exinfo = self._get_exchange_info_cached()
        if not exinfo:
            self._symbol_rule_cache[sym] = rules
            return rules

        try:
            for s in exinfo.get("symbols", []):
                if s.get("symbol") != sym:
                    continue
                for f in s.get("filters", []):
                    ftype = f.get("filterType")
                    if ftype == "PRICE_FILTER":
                        rules["tickSize"] = f.get("tickSize")
                    elif ftype in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                        # 优先 LOT_SIZE（通常用于限价），没有就用 MARKET_LOT_SIZE
                        if rules["stepSize"] is None:
                            rules["stepSize"] = f.get("stepSize")
                break
        except Exception as e:
            logger.warning(f"解析交易规则失败(将使用 fallback 精度): {e}")

        self._symbol_rule_cache[sym] = rules
        return rules

    def _align_to_step(self, value: float, step: Optional[str]) -> float:
        """把 value 对齐到 step 的整数倍。

        采用向下取整 ROUND_DOWN 最稳妥：保证不会因越界触发 -4014。
        """
        if value is None or not step or step == "0":
            return value
        v = Decimal(str(value))
        s = Decimal(str(step))
        aligned = (v / s).to_integral_value(rounding=ROUND_DOWN) * s
        return float(aligned)
