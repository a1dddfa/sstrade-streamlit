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


class RateLimitMixin:
    def _is_in_rate_limit_cooldown(self) -> bool:
        """检查当前是否处于限流冷却期。

        Returns:
            True: 仍在冷却期，调用方应跳过真实 Binance API 请求
            False: 不在冷却期（若冷却期刚结束，会自动重置计数/时间戳）
        """
        try:
            now = time.time()
            if self.rate_limited_until and now < float(self.rate_limited_until):
                remaining = int(float(self.rate_limited_until) - now)
                logger.warning(
                    f"[RATE LIMIT] 当前处于限流冷却期，还需 {remaining} 秒，跳过本次 Binance API 调用"
                )
                return True

            # 冷却期结束：重置
            if self.rate_limited_until and now >= float(self.rate_limited_until):
                self.rate_limited_until = 0.0
                self.consecutive_1003 = 0
            return False
        except Exception:
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

    def _enter_account_rest_degraded_mode(self, reason: str = "", hold_sec: float = 300.0) -> None:
        """
        进入“账户 REST 降频”模式：
        - 用于 user stream 长时间失败时，避免 positions/balance/open_orders 高频 REST 轮询导致 -1003
        - hold_sec：最低保持时间，避免频繁进出
        """
        now = time.time()
        self._account_rest_degraded = True
        self._user_stream_failed_until = max(self._user_stream_failed_until, now + float(hold_sec))
        logger.error(
            f"[USER_STREAM_DEGRADED] enter degraded mode for {int(hold_sec)}s, "
            f"until={int(self._user_stream_failed_until)} reason={reason}"
        )

    def _should_throttle_account_rest(self, endpoint: str) -> bool:
        """
        在 WS 未 ready 时，控制账户类 REST 的最低调用间隔。
        返回 True 表示：本次应跳过真实 REST（调用方应返回缓存/空结果）。
        """
        try:
            if self.dry_run:
                return False

            now = time.time()

            # 若 user stream 已恢复并且 WS account ready，就不做降频限制（直接用 WS 缓存）
            if getattr(self, "_ws_account_ready", False):
                self._account_rest_degraded = False
                self._user_stream_fail_count = 0
                return False

            # 只要仍处于“失败保持期”，就视为降级
            if self._user_stream_failed_until and now < float(self._user_stream_failed_until):
                self._account_rest_degraded = True

            min_interval = (
                self._account_rest_min_interval_degraded
                if self._account_rest_degraded
                else self._account_rest_min_interval_normal
            )

            next_allowed = float(self._account_rest_next_allowed.get(endpoint, 0.0) or 0.0)
            if now < next_allowed:
                return True

            # 允许打一次：刷新下一次允许时间
            self._account_rest_next_allowed[endpoint] = now + float(min_interval)
            return False

        except Exception:
            return False
