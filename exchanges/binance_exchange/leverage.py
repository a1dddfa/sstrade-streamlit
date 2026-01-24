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


class LeverageMixin:
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
