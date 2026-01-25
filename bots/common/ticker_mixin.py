# -*- coding: utf-8 -*-
"""
Ticker subscription mixin for bots.

Extracted from app/streamlit_app.py (commit #4 split).
"""
from __future__ import annotations
from typing import Callable, Optional, Any, Dict


class TickerSubscriptionMixin:
    """Provide subscribe/unsubscribe helper for exchange ticker websocket."""

    def __init__(self):
        self._ticker_symbol: Optional[str] = None
        self._ticker_cb: Optional[Callable[[Any], None]] = None

    def _subscribe_ticker_if_any(self, exchange: Any, symbol: str, cb: Callable[[Any], None]) -> None:
        # 兼容：有些 bot 没调用 mixin.__init__，这里兜底初始化字段
        if not hasattr(self, "_ticker_symbol"):
            self._ticker_symbol = None
        if not hasattr(self, "_ticker_cb"):
            self._ticker_cb = None

        self._ticker_symbol = symbol
        self._ticker_cb = cb
        try:
            exchange.ws_subscribe_ticker(symbol, cb)
        except Exception as e:
            # subscribe failures shouldn't crash bot init
            raise e

    def _unsubscribe_ticker_if_any(self, exchange: Any) -> None:
        if not hasattr(self, "_ticker_symbol"):
            self._ticker_symbol = None
        if not hasattr(self, "_ticker_cb"):
            self._ticker_cb = None

        if not self._ticker_symbol:
            return
        try:
            exchange.ws_unsubscribe_ticker(self._ticker_symbol, self._ticker_cb)
        except Exception:
            try:
                exchange.ws_unsubscribe_ticker(self._ticker_symbol)
            except Exception:
                pass
        finally:
            self._ticker_symbol = None
            self._ticker_cb = None

    def _ensure_ticker_ws(
        self,
        exchange: Any,
        symbol: str,
        factory: Callable[[], Callable[[Dict[str, Any]], None]],
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        确保 ticker WS 已订阅且不会重复订阅：
        - symbol 变化：先退订旧的，再订阅新的
        - symbol 不变：不重复订阅
        - subscribe 出错：记录日志，但不让 bot 直接崩（交给上层 fallback REST）
        """
        if not getattr(exchange, "use_ws", False):
            # 项目只轮询：不做任何订阅
            return
        if not hasattr(self, "_ticker_symbol"):
            self._ticker_symbol = None
        if not hasattr(self, "_ticker_cb"):
            self._ticker_cb = None

        # symbol 没变，且已有回调：认为已订阅
        if self._ticker_symbol == symbol and self._ticker_cb is not None:
            return

        # symbol 变了：退订旧的
        if self._ticker_symbol and self._ticker_symbol != symbol:
            try:
                self._unsubscribe_ticker_if_any(exchange)
            except Exception as e:
                if on_log:
                    on_log(f"⚠️ ticker ws 退订失败(忽略): {e}")

        # 订阅新的
        cb = factory()
        try:
            self._subscribe_ticker_if_any(exchange, symbol, cb)
            if on_log:
                on_log(f"📡 ticker ws 已订阅: {symbol}")
        except Exception as e:
            # 订阅失败不崩；让上层等 timeout 后走 REST fallback
            self._ticker_symbol = None
            self._ticker_cb = None
            if on_log:
                on_log(f"⚠️ ticker ws 订阅失败(将回落REST): {symbol} err={e}")
