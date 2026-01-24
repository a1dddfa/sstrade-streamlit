# -*- coding: utf-8 -*-
"""
Streamlit 控制台：扫描高波动交易对 + 手动选择交易对 + 手动下单（市价/限价）+ 可选 TP/SL（价格输入）+ 仓位查看（Hedge）

运行：
    pip install streamlit pyyaml pandas python-binance streamlit-autorefresh
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import threading
import time
import traceback
import queue
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
import yaml
# NOTE: 之前为了让主线程周期性 rerun（从而 drain WS 事件队列）引入了 st_autorefresh。
# 现在改为：在 User Stream WS 回调里直接分发给 bot，不再依赖页面刷新。
# 使用你项目里的交易所封装
# ===== 日志初始化（必须在 Streamlit 入口里）=====
from pathlib import Path
import logging

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"

# 兼容两种放置方式：
# 1) core/logging_config.py（推荐）
# 2) 项目根目录 logging_config.py
try:
    from core.logging_config import setup_logging
except Exception:  # pragma: no cover
    from logging_config import setup_logging  # type: ignore

setup_logging(log_dir=str(LOG_DIR), level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("✅ Streamlit logging initialized, log_dir=%s", LOG_DIR)


# -----------------------------
# User stream 事件：WS 回调线程 -> 直接分发给 bot（不依赖 Streamlit rerun）
# -----------------------------
# Binance user-data-stream 的回调跑在后台线程。
# 这里避免触碰 st.session_state（线程不安全），只使用模块级引用 + 锁。
_BOT_DISPATCH_LOCK = threading.RLock()
_RANGE2_BOT_REF: Optional["RangeTwoBot"] = None
_UI_LOGGER_REF: Optional["UILogger"] = None


def register_range2_bot(bot: Optional["RangeTwoBot"]) -> None:
    """把 RangeTwoBot 引用注册到 WS 回调分发器里（线程安全）。"""
    global _RANGE2_BOT_REF
    with _BOT_DISPATCH_LOCK:
        _RANGE2_BOT_REF = bot


def register_ui_logger(ui_logger: Optional["UILogger"]) -> None:
    """把 UILogger 引用注册到 WS 回调分发器里（线程安全）。"""
    global _UI_LOGGER_REF
    with _BOT_DISPATCH_LOCK:
        _UI_LOGGER_REF = ui_logger


def _make_user_stream_handler_direct():
    """返回一个 *纯 Python* 的 WS 回调：直接调用 bot，不依赖 Streamlit 主线程 drain。"""

    def _handler(o: Dict[str, Any]):
        # 1) 写 UI 日志（线程安全：UILogger 内部有锁）
        try:
            with _BOT_DISPATCH_LOCK:
                ui_logger = _UI_LOGGER_REF
            if ui_logger is not None:
                ui_logger.log(
                    f"[ORDER] {o.get('symbol')} {o.get('status')} "
                    f"side={o.get('side')} posSide={o.get('positionSide')} "
                    f"tag={o.get('tag')} avg={o.get('avgPrice')} clientId={o.get('clientOrderId')}"
                )
        except Exception:
            pass

        # 2) 分发给 RangeTwoBot（线程安全：bot 内部会加锁）
        try:
            with _BOT_DISPATCH_LOCK:
                rb = _RANGE2_BOT_REF
            if rb is not None:
                rb.on_user_stream_order_update(o)
        except Exception:
            # WS 回调里不抛异常，避免影响 WS 主循环
            pass

    return _handler

# 统一优先用 exchanges/ 目录下的版本；如果你的项目没有该包，则回退到根目录版本
try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange

# -----------------------------
# 统一的行情 WS 订阅/精确退订（callback 级别）
# -----------------------------
class TickerSubscriptionMixin:
    """
    统一管理：每个 bot 自己的 ticker callback 生命周期
    - 订阅：exchange.ws_subscribe_ticker(symbol, cb)
    - 精确退订：exchange.ws_unsubscribe_ticker(symbol, callback=cb)
    - 切换 symbol：先退旧 cb，再订新 cb
    """
    def __init__(self):
        self._sub_symbol = None   # 已订阅的 symbol（格式化后）
        self._ticker_cb = None    # 本 bot 的 callback 引用

    def _unsubscribe_ticker_if_any(self, exchange):
        """精确退订：只退本 bot 的 callback，不影响其他订阅者。"""
        if self._sub_symbol and self._ticker_cb:
            try:
                exchange.ws_unsubscribe_ticker(self._sub_symbol, callback=self._ticker_cb)
            except Exception:
                pass
        self._sub_symbol = None
        self._ticker_cb = None

    def _ensure_ticker_ws(self, exchange, symbol: str, on_ticker_factory, on_log=None):
        """
        确保订阅指定 symbol 的行情。
        - symbol 不变：不重复订阅，不替换 callback
        - symbol 变化：精确退订旧 callback，再订阅新 callback
        """
        sym = str(symbol).replace("/", "").upper()
        if self._sub_symbol == sym:
            return

        # 切 symbol：先精确退订旧的
        self._unsubscribe_ticker_if_any(exchange)

        # 只在“需要新订阅”时创建 callback（保证后续能精确退订）
        cb = on_ticker_factory()
        self._ticker_cb = cb
        exchange.ws_subscribe_ticker(sym, cb)
        self._sub_symbol = sym

        if on_log:
            try:
                on_log(f"📡 已订阅行情WS: {sym}")
            except Exception:
                pass


# -----------------------------
# 线程安全日志
# -----------------------------
class UILogger:
    def __init__(self, max_lines: int = 800):
        self.max_lines = int(max_lines)
        self._lock = threading.Lock()
        self._lines: List[str] = []

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self.max_lines:
                self._lines = self._lines[-self.max_lines :]

    def tail(self, n: int = 300) -> str:
        with self._lock:
            lines = self._lines[-int(n) :]
        return "\n".join(lines)


# -----------------------------
# 阶梯机器人（价格触发即下单）
# -----------------------------
@dataclass
class LadderConfig:
    symbol: str = "ETHUSDT"
    side: str = "short"  # short/long（对应你策略：涨多做空 / 跌多做多）
    base_qty: float = 0.01

    step_pct: float = 0.05           # 5% => 0.05；下一档=last*(1±step)
    limit_offset_pct: float = 0.001  # 限价偏移（0.1%）
    tick_interval_sec: float = 1.0

    enable_ladder: bool = True
    enable_tp_reset: bool = True
    tp_pct: float = 0.002            # 0.2% 示例（阶梯模块用）
    tag_prefix: str = "UI_LADDER"


@dataclass
class LadderState:
    running: bool = False
    last_price: Optional[float] = None
    last_entry_price: Optional[float] = None
    next_add_price: Optional[float] = None

    position_amt: float = 0.0
    entry_price: Optional[float] = None
    tp_order_id: Optional[str] = None
    last_entry_ts: Optional[float] = None
    last_pos_sync_ts: Optional[float] = None  # 上次同步持仓时间（限频用）

    last_error: Optional[str] = None


class LadderBot(TickerSubscriptionMixin):
    def __init__(self, exchange: BinanceExchange, ui_logger: UILogger):
        super().__init__()
        self.exchange = exchange
        self.log = ui_logger

        self.cfg = LadderConfig()
        self.state = LadderState()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # --- WS 行情推送：用队列驱动，减少轮询 ---
        self._px_q: "queue.Queue[float]" = queue.Queue(maxsize=1)  # 只保留最新价格
        self._sub_symbol: Optional[str] = None  # 记录已订阅的交易对，避免重复订阅
        # 保存本 bot 的行情回调引用（用于“精确退订”，避免把别的模块的订阅一起退掉）
        self._ticker_cb = None


    def configure(self, cfg: LadderConfig):
        with self._lock:
            self.cfg = cfg
        self.log.log(f"✅ 已更新阶梯参数: {cfg.symbol} side={cfg.side} step={cfg.step_pct*100:.2f}% qty={cfg.base_qty}")

    def start(self):
        if self.state.running:
            return
        self._stop.clear()
        self.state.running = True
        self._thread = threading.Thread(target=self._run, name="LadderBot", daemon=True)
        self._thread.start()
        self.log.log("🚀 LadderBot 已启动")

    def stop(self):
        if not self.state.running:
            return
        self._stop.set()
        self.state.running = False

        # 停止时退订行情 WS（可选，但建议做，避免残留连接）
        self._unsubscribe_ticker_if_any(self.exchange)

        self.log.log("🛑 LadderBot 停止信号已发送")
        
    def _ensure_ticker_ws(self, symbol: str):
        def factory():
            def on_ticker(t: Dict[str, Any]):
                try:
                    px = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
                except Exception:
                    return
                if px <= 0:
                    return

                # 保持队列中只有“最新”一条（丢弃旧的）
                try:
                    while True:
                        self._px_q.get_nowait()
                except Exception:
                    pass

                try:
                    self._px_q.put_nowait(px)
                except Exception:
                    pass

            return on_ticker

        # ✅ 只使用 mixin 的订阅管理：避免重复订阅，并确保 stop() 时能精确退订
        super()._ensure_ticker_ws(self.exchange, symbol, factory, on_log=self.log.log)


    def _calc_next_add_price(self, last_entry: float, side: str, step: float) -> float:
        # short: last*(1+step)；long: last*(1-step)
        if side == "short":
            return last_entry * (1.0 + step)
        return last_entry * (1.0 - step)

    def _entry_limit_price(self, mark: float, side: str, offset: float) -> float:
        # short: 挂高一点卖；long: 挂低一点买
        if side == "short":
            return mark * (1.0 + offset)
        return mark * (1.0 - offset)

    def _get_position_one(self, symbol: str) -> Dict[str, Any]:
        pos = self.exchange._get_ws_positions(symbol) or []
        if isinstance(pos, dict):
            return pos
        if isinstance(pos, list) and pos:
            # 找匹配 symbol 的第一条
            for p in pos:
                s = (p.get("symbol") or p.get("s") or "")
                if str(s).replace("/", "") == str(symbol).replace("/", ""):
                    return p
            return pos[0]
        return {}

    def _place_entry(self, symbol: str, side: str, qty: float, price: float, tag: str):
        self.log.log(f"🟦 下入场单: {symbol} side={side} qty={qty} limit≈{price:.6f} tag={tag}")
        o = self.exchange.create_order(
            symbol=symbol,
            side=side,
            order_type="limit",
            quantity=float(qty),
            price=float(price),
            params={"reduceOnly": False, "timeInForce": "GTC", "tag": tag},
        )
        return o

    def _cancel_tp_if_any(self, symbol: str, tp_order_id: Optional[str]):
        if not tp_order_id:
            return
        try:
            self.exchange.cancel_order(symbol=symbol, order_id=tp_order_id)
            self.log.log(f"🧹 已撤销旧 TP: order_id={tp_order_id}")
        except Exception as e:
            self.log.log(f"⚠️ 撤销 TP 失败(忽略): {e}")

    def _place_tp(self, symbol: str, side: str, qty: float, entry_price: float, tp_pct: float, tag: str) -> Optional[str]:
        # side 是当前仓位方向；TP 是反向 reduceOnly 单
        if qty <= 0 or entry_price <= 0:
            return None
        if side == "short":
            tp_price = entry_price * (1.0 - tp_pct)
            tp_side = "long"   # 平空 => 买
        else:
            tp_price = entry_price * (1.0 + tp_pct)
            tp_side = "short"  # 平多 => 卖

        self.log.log(f"🟩 挂 TP: {symbol} tp_price≈{tp_price:.6f} qty={qty} tag={tag}")
        o = self.exchange.create_order(
            symbol=symbol,
            side=tp_side,
            order_type="limit",
            quantity=float(qty),
            price=float(tp_price),
            params={"reduceOnly": True, "timeInForce": "GTC", "tag": tag},
        )
        oid = o.get("orderId") if isinstance(o, dict) else None
        return str(oid) if oid else None

    def _run(self):
        # ✅ 行情获取策略（WS优先，断流时低频回落REST）
        last_rest_fetch_ts = 0.0
        rest_min_interval = 10.0  # 秒：REST fallback 最小间隔（建议 2~5s）
        ws_wait_timeout = 8.0
        while not self._stop.is_set():
            try:
                with self._lock:
                    cfg = self.cfg

                # 1) 用 WS 推送驱动行情（不轮询）
                self._ensure_ticker_ws(cfg.symbol)

                try:
                    mark = self._px_q.get(timeout=ws_wait_timeout)  # 优先等 WS 推送
                except queue.Empty:
                    # WS 没推送：低频 fallback REST，避免断线导致 BE/锁盈逻辑永远不触发
                    now = time.time()
                    if (now - last_rest_fetch_ts) < rest_min_interval:
                        continue
                    last_rest_fetch_ts = now
                    try:
                        t = self.exchange.get_ticker(cfg.symbol) or {}
                        mark = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
                    except Exception:
                        # REST 也失败：跳过本轮
                        continue

                if mark <= 0:
                    continue

                self.state.last_price = mark

                # 2) 同步持仓（这里取一条 position，用于阶梯模块简单展示）
                # --- 持仓同步限频（避免 WS 高频推送下反复同步） ---
                now = time.time()
                need_sync = (
                    self.state.last_pos_sync_ts is None
                    or (now - self.state.last_pos_sync_ts) >= 1.0
                )

                if need_sync:
                    p = self._get_position_one(cfg.symbol)
                    self.state.last_pos_sync_ts = now
                else:
                    # 复用上一次持仓结果
                    p = getattr(self.state, "_cached_position", {}) or {}

                # 缓存一份，供下次限频时使用
                self.state._cached_position = p

                try:
                    amt = float(p.get("positionAmt") or p.get("amt") or 0.0)
                except Exception:
                    amt = 0.0
                self.state.position_amt = amt

                try:
                    ep = float(p.get("entryPrice") or p.get("entry_price") or 0.0)
                except Exception:
                    ep = 0.0
                self.state.entry_price = ep if ep > 0 else None

                # 3) 初始化锚点
                if self.state.last_entry_price is None:
                    self.state.last_entry_price = mark
                if self.state.next_add_price is None:
                    self.state.next_add_price = self._calc_next_add_price(self.state.last_entry_price, cfg.side, cfg.step_pct)

                # 4) 如果没有仓位：先下第一单（限价）
                if abs(amt) <= 0:
                    # 防止 WS 高频推送导致“第一单”在仓位尚未更新时重复下
                    if self.state.last_entry_ts and (time.time() - self.state.last_entry_ts) < 3.0:
                        continue
                    entry_price = self._entry_limit_price(mark, cfg.side, cfg.limit_offset_pct)
                    tag = f"{cfg.tag_prefix}_ENTRY"
                    self._place_entry(cfg.symbol, cfg.side, cfg.base_qty, entry_price, tag)
                    self.state.last_entry_price = mark
                    self.state.next_add_price = self._calc_next_add_price(mark, cfg.side, cfg.step_pct)
                    self.log.log(f"🧭 已放置第一单，next_add≈{self.state.next_add_price:.6f}")
                    self.state.last_entry_ts = time.time()
                    continue

                # 5) 已有仓位：价格触发则加仓
                if cfg.enable_ladder and self.state.next_add_price is not None:
                    should_add = (cfg.side == "short" and mark >= float(self.state.next_add_price)) or \
                                 (cfg.side == "long" and mark <= float(self.state.next_add_price))
                    if should_add:
                        entry_price = self._entry_limit_price(mark, cfg.side, cfg.limit_offset_pct)
                        tag = f"{cfg.tag_prefix}_ADD"
                        self._place_entry(cfg.symbol, cfg.side, cfg.base_qty, entry_price, tag)
                        self.state.last_entry_price = mark
                        self.state.next_add_price = self._calc_next_add_price(mark, cfg.side, cfg.step_pct)
                        self.log.log(f"➕ 加仓触发：mark={mark:.6f} next_add→{self.state.next_add_price:.6f}")

                        # 6) 每次加仓后：重设 TP（阶梯模块自己的 TP）
                        if cfg.enable_tp_reset and self.state.entry_price:
                            qty_abs = abs(float(amt))
                            self._cancel_tp_if_any(cfg.symbol, self.state.tp_order_id)
                            self.state.tp_order_id = self._place_tp(
                                cfg.symbol, cfg.side, qty_abs, float(self.state.entry_price), cfg.tp_pct,
                                tag=f"{cfg.tag_prefix}_TP"
                            )


            except Exception as e:
                self.state.last_error = str(e)
                self.log.log(f"❌ LadderBot 异常: {e}")
                self.log.log(traceback.format_exc())
                time.sleep(2)


# -----------------------------
# 区间两单策略：
# - 输入两档价格（自动取高/低）
# - A1：在两价中点挂第一单（限价），SL=低价(做多)/高价(做空)，TP=高价±(差/2)
# - A2：在当前价下方/上方固定百分比挂第二单（可调）
# - 当 A1 成交后：价格达到“高价(做多)/低价(做空)”时，挂一个 closePosition 的 STOP_MARKET，止损价= A1 挂单价 ±0.1%
# - 当 A2 成交后：价格达到“高价+差/4(做多)/低价-差/4(做空)”时，挂 closePosition STOP_MARKET，止损价= A2 挂单价 ±0.1%
#
# 说明：这里的“止损价”是用于保本/锁盈的触发价（全部止损 closePosition）。
# -----------------------------

@dataclass
class RangeTwoConfig:
    symbol: str = "ETHUSDT"
    side: str = "long"  # long/short
    qty1: float = 0.01
    qty2: float = 0.01
    price_a: float = 0.0
    price_b: float = 0.0
    second_entry_offset_pct: float = 0.01   # 1% => 0.01
    be_offset_pct: float = 0.001            # 0.1% => 0.001
    tick_interval_sec: float = 1.0
    tag_prefix: str = "UI_RANGE2"


@dataclass
class RangeTwoState:
    running: bool = False
    last_price: Optional[float] = None

    low: Optional[float] = None
    high: Optional[float] = None
    diff: Optional[float] = None

    a1_limit_price: Optional[float] = None
    a2_limit_price: Optional[float] = None

    # 实际入场均价（A2 改为市价后需要以成交均价作为后续 BE 计算基准）
    a1_entry_price: Optional[float] = None
    a2_entry_price: Optional[float] = None

    tp_price: Optional[float] = None
    sl_price: Optional[float] = None

    tp1_placed: bool = False
    sl1_placed: bool = False
    tp2_placed: bool = False
    sl2_placed: bool = False

    a1_client_id: Optional[str] = None
    a2_client_id: Optional[str] = None

    a1_filled: bool = False
    a2_filled: bool = False

    be1_placed: bool = False
    be2_placed: bool = False

    # ✅ 全局 BE 单（以“当前仓位损益两平价(BEP)”为基准）
    be_order_id: Optional[str] = None   # 已挂出的 BE 保护单 orderId（用于撤旧换新）
    last_bep: Optional[float] = None    # 上一次计算的 BEP（用于检测 A2 成交后均价变化）

    # 防抖：避免在网络抖动/下单失败时，每个 tick 都重复补挂 TP/SL
    last_protect_attempt_ts1: float = 0.0
    last_protect_attempt_ts2: float = 0.0

    last_error: Optional[str] = None


class RangeTwoBot(TickerSubscriptionMixin):
    def __init__(self, exchange: "BinanceExchange", ui_logger: "UILogger"):
        super().__init__()
        self.exchange = exchange
        self.log = ui_logger

        self.cfg = RangeTwoConfig()
        self.state = RangeTwoState()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._px_q: "queue.Queue[float]" = queue.Queue(maxsize=1)
        # WS 断流时的 REST 回落限频
        self._last_rest_ts: float = 0.0

        # ✅ 最近一次非零仓位缓存：避免 REST 降级/限流时误读为 0 导致 BE 不挂
        self._last_qty_cache: Dict[str, float] = {}


    def configure(self, cfg: RangeTwoConfig):
        with self._lock:
            self.cfg = cfg

        a = float(cfg.price_a)
        b = float(cfg.price_b)
        lo, hi = (a, b) if a <= b else (b, a)
        d = hi - lo
        self.state.low, self.state.high, self.state.diff = lo, hi, d

        self.log.log(
            f"✅ 已更新区间两单参数: {cfg.symbol} side={cfg.side} qty1={cfg.qty1} qty2={cfg.qty2} "
            f"range=({lo}, {hi}) offset2={cfg.second_entry_offset_pct*100:.2f}% be={cfg.be_offset_pct*100:.2f}%"
        )

    def start(self):
        if self.state.running:
            return
        self._stop.clear()
        self.state.running = True
        self._thread = threading.Thread(target=self._run, name="RangeTwoBot", daemon=True)
        self._thread.start()
        self.log.log("🚀 RangeTwoBot 已启动")

    def stop(self):
        if not self.state.running:
            return
        self._stop.set()
        self.state.running = False

        self._unsubscribe_ticker_if_any(self.exchange)

        self.log.log("🛑 RangeTwoBot 停止信号已发送")


    def reset_runtime_flags(self):
        self.state.a1_client_id = None
        self.state.a2_client_id = None
        self.state.a1_filled = False
        self.state.a2_filled = False
        self.state.be1_placed = False
        self.state.be2_placed = False
        self.state.a1_limit_price = None
        self.state.a2_limit_price = None
        self.state.a1_entry_price = None
        self.state.a2_entry_price = None
        self.state.tp_price = None
        self.state.sl_price = None
        self.state.tp1_placed = False
        self.state.sl1_placed = False
        self.state.tp2_placed = False
        self.state.sl2_placed = False
        self.state.last_error = None
        self.state.be_order_id = None
        self.state.last_bep = None


    def _ensure_ticker_ws(self, symbol: str):
        def factory():
            def on_ticker(t: Dict[str, Any]):
                try:
                    px = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
                except Exception:
                    return
                if px <= 0:
                    return

                try:
                    while True:
                        self._px_q.get_nowait()
                except Exception:
                    pass

                try:
                    self._px_q.put_nowait(px)
                except Exception:
                    pass

            return on_ticker

        # ✅ 只使用 mixin 的订阅管理：避免重复订阅，并确保 stop() 时能精确退订
        super()._ensure_ticker_ws(self.exchange, symbol, factory, on_log=self.log.log)

    def _place_limit_with_fixed_tp_sl(
        self,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        sl_price: float,
        tp_price: float,
        tag: str,
    ) -> Dict[str, Any]:
        position_side = "LONG" if side == "long" else "SHORT"
        params = {
            "timeInForce": "GTC",
            "tag": tag,
            "positionSide": position_side,

            # ✅ 关键：交给 BinanceExchange.create_order 处理
            "take_profit": {"price": float(tp_price)} if float(tp_price) > 0 else None,
            "stop_loss": {"price": float(sl_price)} if float(sl_price) > 0 else None,
        }
        # 清掉 None，避免传进 processed_params 里造成歧义
        params = {k: v for k, v in params.items() if v is not None}

        self.log.log(
            f"🟦 下区间限价单: {symbol} side={side} qty={qty} limit≈{limit_price:.6f} "
            f"SL={sl_price:.6f} TP={tp_price:.6f} tag={tag}"
        )
        return self.exchange.create_order(
            symbol=symbol,
            side=side,
            order_type="limit",
            quantity=float(qty),
            price=float(limit_price),
            params=params,
        )


    def place_initial_orders(self):
        with self._lock:
            cfg = self.cfg

        if cfg.price_a <= 0 or cfg.price_b <= 0:
            raise ValueError("price_a/price_b 必须 > 0")

        lo = float(self.state.low or 0.0)
        hi = float(self.state.high or 0.0)
        d = float(self.state.diff or 0.0)
        if lo <= 0 or hi <= 0 or d <= 0:
            raise ValueError("两档价格必须不同且 > 0")

        # 下单不依赖 WS：优先用交易所封装里的 ticker（通常会优先用 WS 缓存，过期再回落 REST）
        # 若仍失败，则用区间中点兜底，避免“WS 抖动 => 下不了单”。
        mark = 0.0
        try:
            t = self.exchange.get_ticker(cfg.symbol) or {}
            mark = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
        except Exception:
            mark = 0.0
        if mark <= 0:
            mark = (lo + hi) / 2.0


        if cfg.side == "long":
            sl = lo
            tp = hi + d / 2.0
            a1_price = (lo + hi) / 2.0
            a2_price = mark * (1.0 - float(cfg.second_entry_offset_pct))
        else:
            sl = hi
            tp = lo - d / 2.0
            a1_price = (lo + hi) / 2.0
            a2_price = mark * (1.0 + float(cfg.second_entry_offset_pct))

        tag_a1 = f"{cfg.tag_prefix}_A1"
        tag_a2 = f"{cfg.tag_prefix}_A2"

        # ===== 先写“计划参数”（TP/SL/价格/标志位）再下单，进一步减少竞态窗口 =====
        with self._lock:
            self.state.a1_limit_price = float(a1_price)
            self.state.a2_limit_price = float(a2_price)
            self.state.tp_price = float(tp)
            self.state.sl_price = float(sl)

            # 每次新开一轮，重置挂单标志（实际成交后会置 True）
            self.state.tp1_placed = False
            self.state.sl1_placed = False
            self.state.tp2_placed = False
            self.state.sl2_placed = False

        # A1：仍然用限价挂单
        o1 = self._place_limit_with_fixed_tp_sl(cfg.symbol, cfg.side, cfg.qty1, a1_price, sl, tp, tag_a1)

        # ✅ A1 下完立刻写入 clientId（缩小竞态窗口）
        with self._lock:
            self.state.a1_client_id = str((o1 or {}).get("clientOrderId") or "") or None

        # A2：市价单
        position_side = "LONG" if cfg.side == "long" else "SHORT"
        self.log.log(
            f"🟦 下 A2 市价单: {cfg.symbol} side={cfg.side} qty={cfg.qty2} （参考价≈{a2_price:.6f}） SL={sl:.6f} TP={tp:.6f} tag={tag_a2}"
        )
        o2 = self.exchange.create_order(
            symbol=cfg.symbol,
            side=cfg.side,
            order_type="market",
            quantity=float(cfg.qty2),
            price=None,
            params={
                "tag": tag_a2,
                "positionSide": position_side,

                # ✅ 关键：交给 BinanceExchange.create_order 处理
                "take_profit": {"price": float(tp)} if float(tp) > 0 else None,
                "stop_loss": {"price": float(sl)} if float(sl) > 0 else None,
            },
        )


        # ✅ A2 下完立刻写入 clientId
        with self._lock:
            self.state.a2_client_id = str((o2 or {}).get("clientOrderId") or "") or None

        self.log.log(
            f"📌 已下 A1/A2：A1@{a1_price:.6f} clientId={self.state.a1_client_id}；"
            f"A2@{a2_price:.6f} clientId={self.state.a2_client_id}"
        )


    def on_user_stream_order_update(self, order: Dict[str, Any]):
        """
        从 user stream 收到订单更新后，识别 A1/A2 是否成交。
        注意：BinanceExchange 会把 tag 从 clientOrderId 里 split('_')[0]，所以不要依赖 order['tag'] 来判断 A1/A2。
        """
        try:
            status = (order.get("status") or "").upper()
            if status != "FILLED":
                return

            cid = str(order.get("clientOrderId") or "")

            # WS 回调线程会直接调用本方法：用锁保护 state/cfg 读写，避免与 _run 线程竞争。
            do_ensure_protection = False
            with self._lock:
                cfg = self.cfg

                # 1) 优先用“精确 clientOrderId 匹配”
                if self.state.a1_client_id and cid == self.state.a1_client_id:
                    if self.state.a1_filled:
                        return
                    self.state.a1_filled = True
                    avg = float(order.get("avgPrice") or 0.0)
                    self.state.a1_entry_price = avg if avg > 0 else self.state.a1_limit_price

                    # 注意：精确匹配分支会走“锁外实际下单”，这里不要触发补挂，避免重复下保护单
                    # （补挂只留给兜底分支触发）

                    close_side = "short" if cfg.side == "long" else "long"
                    pos_side = "LONG" if cfg.side == "long" else "SHORT"

                    # 先释放锁再下单，避免网络调用期间阻塞其他逻辑
                    sl_price = float(self.state.sl_price) if self.state.sl_price else 0.0
                    tp_price = float(self.state.tp_price) if self.state.tp_price else 0.0
                    sl_needed = (not self.state.sl1_placed) and sl_price > 0
                    tp_needed = (not self.state.tp1_placed) and tp_price > 0
                    
                elif self.state.a2_client_id and cid == self.state.a2_client_id:
                    if self.state.a2_filled:
                        return
                    self.state.a2_filled = True
                    avg = float(order.get("avgPrice") or 0.0)
                    self.state.a2_entry_price = avg if avg > 0 else self.state.a2_limit_price

                    # 精确匹配分支不触发补挂，避免重复保护单

                    close_side = "short" if cfg.side == "long" else "long"
                    pos_side = "LONG" if cfg.side == "long" else "SHORT"

                    sl_price = float(self.state.sl_price) if self.state.sl_price else 0.0
                    tp_price = float(self.state.tp_price) if self.state.tp_price else 0.0
                    sl_needed = (not self.state.sl2_placed) and sl_price > 0
                    tp_needed = (not self.state.tp2_placed) and tp_price > 0
                else:
                    # 2) 兜底：如果未来你重启丢了 state.client_id，也能用 cid 包含判断
                    if "_A1_" in cid and (not self.state.a1_filled):
                        self.state.a1_filled = True
                        avg = float(order.get("avgPrice") or 0.0)
                        self.state.a1_entry_price = avg if avg > 0 else self.state.a1_limit_price
                        self.log.log(
                            f"✅ A1 成交(兜底): cid={cid} avg={order.get('avgPrice')} executed={order.get('executedQty')}"
                        )
                        do_ensure_protection = True
                    if "_A2_" in cid and (not self.state.a2_filled):
                        self.state.a2_filled = True
                        avg = float(order.get("avgPrice") or 0.0)
                        self.state.a2_entry_price = avg if avg > 0 else self.state.a2_limit_price
                        self.log.log(
                            f"✅ A2 成交(兜底): cid={cid} avg={order.get('avgPrice')} executed={order.get('executedQty')}"
                        )
                        do_ensure_protection = True

                    # 兜底命中后也继续往下走：让统一的补挂逻辑来决定是否需要挂 TP/SL

            # 如果是成交事件，且存在“未补挂”的可能性，则立刻尝试补挂（不依赖 UI 刷新/下一次 tick）
            if do_ensure_protection:
                self._ensure_tp_sl_if_needed()

            # ===== 锁外：实际下单 =====
            if self.state.a1_client_id and cid == self.state.a1_client_id:
                self.log.log(
                    f"✅ A1 成交: cid={cid} avg={order.get('avgPrice')} executed={order.get('executedQty')}"
                )

                if sl_needed:
                    self._place_close_stop_market(
                        cfg.symbol,
                        close_side,
                        float(cfg.qty1),
                        float(sl_price),
                        pos_side,
                        f"MANUAL_{cfg.tag_prefix}_SL1_STOPMKT",
                    )
                    with self._lock:
                        self.state.sl1_placed = True
                    self.log.log(f"🛡️ SL1 已挂 STOP_MARKET：sl={float(sl_price):.6f}")

                if tp_needed:
                    self._place_close_limit(
                        cfg.symbol,
                        close_side,
                        float(cfg.qty1),
                        float(tp_price),
                        f"MANUAL_{cfg.tag_prefix}_TP1_LIMIT",
                    )
                    with self._lock:
                        self.state.tp1_placed = True
                    self.log.log(f"🎯 TP1 已挂 LIMIT：tp={float(tp_price):.6f}")
                return

            if self.state.a2_client_id and cid == self.state.a2_client_id:
                self.log.log(
                    f"✅ A2 成交: cid={cid} avg={order.get('avgPrice')} executed={order.get('executedQty')}"
                )

                if sl_needed:
                    self._place_close_stop_market(
                        cfg.symbol,
                        close_side,
                        float(cfg.qty2),
                        float(sl_price),
                        pos_side,
                        f"MANUAL_{cfg.tag_prefix}_SL2_STOPMKT",
                    )
                    with self._lock:
                        self.state.sl2_placed = True
                    self.log.log(f"🛡️ SL2 已挂 STOP_MARKET：sl={float(sl_price):.6f}")

                if tp_needed:
                    self._place_close_limit(
                        cfg.symbol,
                        close_side,
                        float(cfg.qty2),
                        float(tp_price),
                        f"MANUAL_{cfg.tag_prefix}_TP2_LIMIT",
                    )
                    with self._lock:
                        self.state.tp2_placed = True
                    self.log.log(f"🎯 TP2 已挂 LIMIT：tp={float(tp_price):.6f}")
                return

        except Exception as e:
            with self._lock:
                self.state.last_error = str(e)
                
    def _get_abs_position_qty(self, symbol: str, side: Optional[str] = None) -> float:
        """
        ✅ 获取“当前真实仓位量”的绝对值（用于 BE 单数量）

        修复点：
        - 优先用 WS positions（更及时，不容易被 REST 节流影响）
        - 尽量按 positionSide (LONG/SHORT) 过滤并汇总（Hedge 模式更可靠）
        - REST 在降级/限流返回 [] 或误返回 0 时，回退到“最近一次非零仓位缓存”，避免误判为 0
        """
        sym = str(symbol).replace("/", "").upper()

        # 期望的 positionSide（Hedge 模式）
        want_ps = None
        if side == "long":
            want_ps = "LONG"
        elif side == "short":
            want_ps = "SHORT"

        cache_key = f"{sym}:{want_ps or 'BOTH'}"

        def _sum_abs_amt(pos_list) -> float:
            if not pos_list:
                return 0.0
            if isinstance(pos_list, dict):
                pos_list = [pos_list]
            total = 0.0
            for p in pos_list:
                s = str(p.get("symbol") or p.get("s") or "").replace("/", "").upper()
                if s != sym:
                    continue
                ps = str(p.get("positionSide") or p.get("ps") or "").upper()
                if want_ps and ps and ps != want_ps:
                    continue
                try:
                    amt = float(p.get("positionAmt") or p.get("amt") or 0.0)
                except Exception:
                    amt = 0.0
                total += abs(amt)
            return float(total)

        # 0) WS 优先（更快更准）
        try:
            ws_pos = self.exchange._get_ws_positions(sym)  # UI 里也在用这个
            qty = _sum_abs_amt(ws_pos)
            if qty > 0:
                self._last_qty_cache[cache_key] = qty
                return qty
        except Exception:
            pass

        # 1) REST fallback（可能被降级节流/冷却期影响）
        try:
            rest_pos = self.exchange.get_positions(sym) or []
            qty = _sum_abs_amt(rest_pos)
            if qty > 0:
                self._last_qty_cache[cache_key] = qty
                return qty
        except Exception:
            pass

        # 2) 再 fallback：使用“最近一次非零仓位缓存”
        cached = self._last_qty_cache.get(cache_key)
        if cached is not None:
            return float(cached)

        return 0.0


    def _calc_current_bep(self, cfg: "RangeTwoConfig") -> Optional[float]:
        """
        ✅ 计算“当前仓位损益两平价(BEP)”：
        - 只有 A1：BEP = A1 成交均价（或退回 A1 限价）
        - 只有 A2：BEP = A2 成交均价（或退回 A2 参考价）
        - A1 + A2：BEP = 加权均价（qty1/qty2 权重）
        """
        s = self.state

        parts = []

        if s.a1_filled:
            p1 = float(s.a1_entry_price or 0.0) or float(s.a1_limit_price or 0.0)
            q1 = float(cfg.qty1 or 0.0)
            if p1 > 0 and q1 > 0:
                parts.append((p1, q1))

        if s.a2_filled:
            p2 = float(s.a2_entry_price or 0.0) or float(s.a2_limit_price or 0.0)
            q2 = float(cfg.qty2 or 0.0)
            if p2 > 0 and q2 > 0:
                parts.append((p2, q2))

        if not parts:
            return None

        num = sum(p * q for p, q in parts)
        den = sum(q for _, q in parts)
        if den <= 0:
            return None
        return num / den

    def _ensure_tp_sl_if_needed(self) -> None:
        """兜底补挂：
        - 识别到 A1/A2 已成交
        - 但 TP/SL 还没创建
        则补挂相应 TP/SL。

        这用于修复以下情况：
        1) 成交 WS 回来太快，state.a*_client_id 还没写好，导致走兜底分支只置 filled 不挂单
        2) UI/进程重启导致 state 中的 *_placed 标志丢失
        """

        now = time.time()
        with self._lock:
            cfg = self.cfg
            s = self.state

            sl_price = float(s.sl_price) if s.sl_price else 0.0
            tp_price = float(s.tp_price) if s.tp_price else 0.0
            if sl_price <= 0 and tp_price <= 0:
                return

            close_side = "short" if cfg.side == "long" else "long"
            pos_side = "LONG" if cfg.side == "long" else "SHORT"

            # 防抖间隔：同一腿 2 秒内只尝试一次
            min_interval = 2.0

            need1_sl = s.a1_filled and (not s.sl1_placed) and sl_price > 0 and (now - float(s.last_protect_attempt_ts1)) >= min_interval
            need1_tp = s.a1_filled and (not s.tp1_placed) and tp_price > 0 and (now - float(s.last_protect_attempt_ts1)) >= min_interval
            need2_sl = s.a2_filled and (not s.sl2_placed) and sl_price > 0 and (now - float(s.last_protect_attempt_ts2)) >= min_interval
            need2_tp = s.a2_filled and (not s.tp2_placed) and tp_price > 0 and (now - float(s.last_protect_attempt_ts2)) >= min_interval

            if not (need1_sl or need1_tp or need2_sl or need2_tp):
                return

            # 先更新时间戳，避免并发重复触发
            if need1_sl or need1_tp:
                s.last_protect_attempt_ts1 = now
            if need2_sl or need2_tp:
                s.last_protect_attempt_ts2 = now

            symbol = cfg.symbol
            qty1 = float(cfg.qty1)
            qty2 = float(cfg.qty2)
            tag_prefix = cfg.tag_prefix

        # ===== 锁外：真正下单（避免网络调用阻塞锁） =====
        try:
            if need1_sl:
                self._place_close_stop_market(
                    symbol,
                    close_side,
                    qty1,
                    float(sl_price),
                    pos_side,
                    f"MANUAL_{tag_prefix}_SL1_STOPMKT",
                )
                with self._lock:
                    self.state.sl1_placed = True
                self.log.log(f"🛡️(补挂) SL1 已挂 STOP_MARKET：sl={float(sl_price):.6f}")

            if need1_tp:
                self._place_close_limit(
                    symbol,
                    close_side,
                    qty1,
                    float(tp_price),
                    f"MANUAL_{tag_prefix}_TP1_LIMIT",
                )
                with self._lock:
                    self.state.tp1_placed = True
                self.log.log(f"🎯(补挂) TP1 已挂 LIMIT：tp={float(tp_price):.6f}")

            if need2_sl:
                self._place_close_stop_market(
                    symbol,
                    close_side,
                    qty2,
                    float(sl_price),
                    pos_side,
                    f"MANUAL_{tag_prefix}_SL2_STOPMKT",
                )
                with self._lock:
                    self.state.sl2_placed = True
                self.log.log(f"🛡️(补挂) SL2 已挂 STOP_MARKET：sl={float(sl_price):.6f}")

            if need2_tp:
                self._place_close_limit(
                    symbol,
                    close_side,
                    qty2,
                    float(tp_price),
                    f"MANUAL_{tag_prefix}_TP2_LIMIT",
                )
                with self._lock:
                    self.state.tp2_placed = True
                self.log.log(f"🎯(补挂) TP2 已挂 LIMIT：tp={float(tp_price):.6f}")

        except Exception as e:
            with self._lock:
                self.state.last_error = str(e)
            self.log.log(f"⚠️ TP/SL 补挂失败（将于后续 tick 重试）：{e}")

    def _place_close_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        tag: str,
    ):
        self.log.log(
            f"🟨 下平仓 LIMIT: {symbol} side={side} qty={qty} price≈{price:.6f} tag={tag}"
        )
        return self.exchange.create_order(
            symbol=symbol,
            side=side,
            order_type="limit",
            quantity=float(qty),
            price=float(price),
            params={
                "reduceOnly": True,
                "timeInForce": "GTC",
                "tag": tag,
            },
        )


    def _place_close_stop_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_price: float,
        position_side: str,
        tag: str,
    ):
        """平仓止损：STOP_MARKET（市价触发）。"""
        self.log.log(
            f"🟥 下平仓 STOP_MARKET: {symbol} side={side} qty={qty} stop≈{stop_price:.6f} posSide={position_side} tag={tag}"
        )
        return self.exchange.create_order(
            symbol=symbol,
            side=side,
            order_type="stop",  # -> STOP_MARKET
            quantity=float(qty),
            price=None,
            params={
                "reduceOnly": True,
                "stopPrice": float(stop_price),
                "positionSide": position_side,
                "tag": tag,
            },
        )

    def _place_close_stop_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_price: float,
        limit_price: float,
        position_side: str,
        tag: str,
    ):
        """保本/锁盈止损：STOP_LIMIT（触发后挂限价）。"""
        self.log.log(
            f"🟧 下平仓 STOP_LIMIT: {symbol} side={side} qty={qty} stop≈{stop_price:.6f} limit≈{limit_price:.6f} posSide={position_side} tag={tag}"
        )
        return self.exchange.create_order(
            symbol=symbol,
            side=side,
            order_type="stop_limit",  # -> STOP
            quantity=float(qty),
            price=float(limit_price),
            params={
                "reduceOnly": True,
                "stopPrice": float(stop_price),
                "timeInForce": "GTC",
                "positionSide": position_side,
                "tag": tag,
            },
        )




    def _place_closepos_stop(self, symbol: str, pos_side: str, stop_side: str, stop_price: float, tag: str):
        self.log.log(
            f"🟥 补挂 closePosition STOP_MARKET: {symbol} side={stop_side} posSide={pos_side} stop≈{stop_price:.6f} tag={tag}"
        )
        return self.exchange.create_order(
            symbol=symbol,
            side=stop_side,
            order_type="stop",  # BinanceExchange 内会映射到 STOP_MARKET
            quantity=0.0,       # closePosition 模式下会被底层剔除
            price=None,
            params={
                "stopPrice": float(stop_price),
                "closePosition": True,
                "positionSide": pos_side,
                "tag": tag,
            },
        )

    def _run(self):
        # ✅ 行情获取策略
        # - 优先 WS（_px_q）
        # - WS 断线/无推送时，低频 fallback REST（exchange.get_ticker）
        last_rest_fetch_ts = 0.0
        rest_min_interval = 10.0  # 秒：REST fallback 最小间隔（建议 2~5s）

        while not self._stop.is_set():
            try:
                with self._lock:
                    cfg = self.cfg

                self._ensure_ticker_ws(cfg.symbol)

                try:
                    mark = self._px_q.get(timeout=2.0)
                except queue.Empty:
                    # WS 断流/重连时：回落 REST（限频），保证 BE/锁盈逻辑不会“永远等不到行情”
                    now = time.time()
                    if (now - float(self._last_rest_ts)) < rest_min_interval:
                        continue
                    self._last_rest_ts = now
                    try:
                        t = self.exchange.get_ticker(cfg.symbol) or {}
                        mark = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
                    except Exception:
                        continue
                    if mark <= 0:
                        continue


                if mark <= 0:
                    continue

                self.state.last_price = mark

                lo = float(self.state.low or 0.0)
                hi = float(self.state.high or 0.0)
                d = float(self.state.diff or 0.0)
                if lo <= 0 or hi <= 0 or d <= 0:
                    continue

                if cfg.side == "long":
                    trg1 = hi
                    trg2 = hi + d / 4.0
                    pos_side = "LONG"
                    stop_side = "short"  # 平多用卖
                    be_mul = 1.0 + float(cfg.be_offset_pct)
                else:
                    trg1 = lo
                    trg2 = lo - d / 4.0
                    pos_side = "SHORT"
                    stop_side = "long"   # 平空用买
                    be_mul = 1.0 - float(cfg.be_offset_pct)

                # ✅ 兜底补挂：只要识别到 A1/A2 已成交，但 TP/SL 还没创建，就补挂。
                # 这可以修复“成交回报太快/页面不刷新/重启丢状态”等导致的漏挂。
                self._ensure_tp_sl_if_needed()

                # =========================
                # ✅ 新 BE 逻辑（按“当前损益两平价(BEP)”动态计算）
                # 触发：达到 BEP 再多走 diff/4
                # 下单：在 BEP 往有利方向 0.1%(千分之一) 挂 STOP_LIMIT
                # 且：A2 成交导致 BEP 变化时，撤掉旧 BE 单并按新 BEP 重挂
                # 数量：使用“当前真实仓位量”的绝对值
                # =========================

                bep = self._calc_current_bep(cfg)
                if bep is not None and bep > 0 and d > 0:
                    if cfg.side == "long":
                        trigger = float(bep) + float(d) / 4.0
                        be_price = float(bep) * (1.0 + float(cfg.be_offset_pct))  # 0.001 => +0.1%
                        ok = (mark >= trigger)
                        pos_side = "LONG"
                        stop_side = "short"
                    else:
                        trigger = float(bep) - float(d) / 4.0
                        be_price = float(bep) * (1.0 - float(cfg.be_offset_pct))  # 0.001 => -0.1%
                        ok = (mark <= trigger)
                        pos_side = "SHORT"
                        stop_side = "long"
                    
                    # [DEBUG] 只有在还没挂 BE 单时才打印调试信息，避免刷屏
                    if not self.state.be_order_id:
                        debug_qty = float(self._get_abs_position_qty(cfg.symbol, side=cfg.side))
                        # 为了避免日志爆炸，仅当价格接近触发价(例如 差距 < 0.5% diff) 或 已经满足 ok 时才打印
                        dist_ratio = abs(mark - trigger) / d
                        if ok or dist_ratio < 0.1: 
                            self.log.log(
                                f"🔍 BE DEBUG: Mark={mark:.4f} Trigger={trigger:.4f} OK={ok} "
                                f"BEP={bep:.4f} Qty={debug_qty:.6f} PosSide={pos_side}"
                            )

                    # --- 如果 BEP 变化且已经挂过 BE 单：撤旧换新 ---
                    if self.state.be_order_id and self.state.last_bep:
                        rel = abs(float(bep) - float(self.state.last_bep)) / float(self.state.last_bep)
                        if rel > 1e-6:
                            try:
                                self.exchange.cancel_order(
                                    symbol=cfg.symbol,
                                    order_id=str(self.state.be_order_id),
                                )
                                self.log.log(
                                    f"♻️ BEP 变化，撤旧 BE：order_id={self.state.be_order_id} "
                                    f"old_bep={self.state.last_bep:.6f} new_bep={float(bep):.6f}"
                                )
                            except Exception as e:
                                self.log.log(f"⚠️ 撤旧 BE 单失败(忽略)：{e}")
                            finally:
                                self.state.be_order_id = None

                    # 更新 last_bep
                    self.state.last_bep = float(bep)

                    # --- 触发后挂 BE 单（若尚未挂）---
                    if ok and (not self.state.be_order_id):
                        qty_abs = float(self._get_abs_position_qty(cfg.symbol, side=cfg.side))
                        if qty_abs > 0:
                            try:
                                o = self._place_close_stop_limit(
                                    cfg.symbol,
                                    stop_side,
                                    qty_abs,           # ✅ 用真实仓位量绝对值
                                    float(be_price),   # stop
                                    float(be_price),   # limit
                                    pos_side,
                                    f"MANUAL_{cfg.tag_prefix}_BE_STOPLIMIT",
                                )
                                oid = (o or {}).get("orderId") if isinstance(o, dict) else None
                                self.state.be_order_id = str(oid) if oid else None

                                self.log.log(
                                    f"🧷 BE 已挂 STOP_LIMIT：trigger={float(trigger):.6f} "
                                    f"bep={float(bep):.6f} be_price={float(be_price):.6f} qty={qty_abs:.6f} "
                                    f"order_id={self.state.be_order_id}"
                                )
                            except Exception as e:
                                self.log.log(f"❌ BE 下单失败: {e}")
                                # 重置错误状态，允许下次重试
                                self.state.be_order_id = None
                        else:
                            # [DEBUG] 明确指出是因为仓位为 0 导致的跳过
                            self.log.log(f"⚠️ BE 触发但仓位获取为 0 (qty={qty_abs})，请检查仓位同步")

                time.sleep(float(cfg.tick_interval_sec))

            except Exception as e:
                self.state.last_error = str(e)
                self.log.log(f"❌ RangeTwoBot 异常: {e}")
                self.log.log(traceback.format_exc())
                time.sleep(2)

# -----------------------------
# 配置读取 / 连接初始化
# -----------------------------
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def init_exchange(cfg: Dict[str, Any], override_dry_run: Optional[bool] = None) -> BinanceExchange:
    ex_cfg = (cfg.get("exchanges") or {}).get("binance") or {}
    g = cfg.get("global") or {}
    if override_dry_run is not None:
        g = dict(g)
        g["dry_run"] = bool(override_dry_run)
    return BinanceExchange(ex_cfg, g)

def render_account_panel(ex, symbol: Optional[str] = None):
    st.subheader("📊 账户状态（WS优先）")

    c1, c2, c3 = st.columns(3)

    with c1:
        st.caption("持仓 Positions")
        pos = ex._get_ws_positions(symbol)
        st.dataframe(pd.DataFrame(pos), use_container_width=True, height=320)

    with c2:
        st.caption("未成交委托 Open Orders")

        try:
            oo = ex._get_ws_open_orders(symbol)
        except Exception as e:
            # 双保险：即便底层还有别的异常，也不让账户页炸
            st.warning(f"获取未成交委托失败：{e}")
            oo = []

        if (symbol is None) and (not oo):
            st.info("当前为“全量未成交(symbol 为空)”视图：若 WS 未就绪，可能会暂时显示为空；可先在下方输入具体 symbol 查看单币对未成交。")

        st.dataframe(pd.DataFrame(oo), use_container_width=True, height=320)


    with c3:
        st.caption("待补挂止损 Pending SL")
        psl = ex.get_pending_stop_losses()
        st.dataframe(pd.DataFrame(psl), use_container_width=True, height=320)

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Trading Control Panel", layout="wide")
st.title("📟 Trading Control Panel（扫描 + 阶梯 + 手动下单）")

with st.sidebar:
    st.header("连接设置")
    cfg_path = st.text_input("config.yaml 路径", value="config.yaml")
    override_dry_run = st.toggle("dry_run（模拟下单）", value=False)

    st.divider()
    st.header("页面")
    page = st.radio(
        "选择功能页",
        options=["🕯 锤子线扫描", "🧩 阶梯 + 手动下单", "🧾 日志", "📊 账户"],
        index=0,
        key="page_select",
    )

    # 以前这里有“全局刷新”用来强制 rerun 以消费 WS 队列。
    # 现在 user stream 直接在 WS 回调里分发给 bot，不再需要刷新开关。

    if st.button("🔌 初始化 / 重新连接", key="init_exchange"):
        try:
            # 1️⃣ 读取配置文件（这里定义 cfg）
            cfg = load_config(cfg_path)
            global_cfg = cfg.get("global") or {}

            # =========================
            # 清理旧 exchange（避免 WS 残留）
            # =========================
            old_ex = st.session_state.get("exchange")
            if old_ex is not None:
                try:
                    old_ex.ws_unsubscribe_user_stream()
                except Exception:
                    pass
                try:
                    old_ex.ws_disconnect()
                except Exception:
                    pass

            # 复位 user-stream 订阅标记
            st.session_state["_user_stream_subscribed"] = False

            # 2️⃣ 创建新 exchange
            new_ex = init_exchange(cfg, override_dry_run=override_dry_run)
            st.session_state["exchange"] = new_ex

            # 3️⃣ 切换 LadderBot / RangeTwoBot 的 exchange 引用
            lb = st.session_state.get("ladder_bot")
            if lb is not None:
                try:
                    lb.stop()
                except Exception:
                    pass
                lb.exchange = new_ex
                lb._sub_symbol = None

            rb = st.session_state.get("range2_bot")
            if rb is not None:
                try:
                    rb.stop()
                except Exception:
                    pass
                rb.exchange = new_ex
                rb._sub_symbol = None

            # ✅ 无论有没有在该页面，都把 bot 注册给 WS 分发器
            register_range2_bot(rb)   # rb 可能为 None，正好也能清理旧引用

            st.success("交易所已初始化 / 已重连（已清理旧 WS）")

            # 4️⃣ 订阅 user stream（只做一次）
            # 直接在 WS 回调里分发给 bot，不再依赖 Streamlit rerun / drain 队列。
            if not st.session_state.get("_user_stream_subscribed"):
                new_ex.ws_subscribe_user_stream(_make_user_stream_handler_direct())
                st.session_state["_user_stream_subscribed"] = True
                st.info("✅ 已订阅用户数据流（订单 / 账户更新）")

        except Exception as e:
            st.error(f"初始化/重连失败：{e}")
            logger.exception("init_exchange failed")


# 初始化默认对象
if "logger" not in st.session_state:
    st.session_state["logger"] = UILogger()
logger: UILogger = st.session_state["logger"]

# 注册到 WS 回调分发器，确保后台线程能写 UI 日志
register_ui_logger(logger)

exchange: Optional[BinanceExchange] = st.session_state.get("exchange")

# ✅ 不再依赖 Streamlit rerun 来消费 user-stream 事件。
# user stream 的 WS 回调会直接分发给 bot。


# ========== Page: Hammer Scanner ==========
if page == "🕯 锤子线扫描":
    st.subheader("扫描：USDT 永续合约的「锤子线 / 倒锤子线」(默认 1h)（可勾选并同步到下单面板）")

    colA, colB, colC = st.columns([1, 1, 1])
    with colA:
        scan_enable = st.toggle("启用扫描刷新（已不推荐，默认关闭）", value=False, key="hammer_scan_enable")
        manual_scan_once = st.button("🔍 手动扫描一次", key="hammer_scan_once")
        interval = st.selectbox("K线周期", options=["5m", "15m", "30m", "1h", "4h", "1d"], index=3)
        lookback_bars = st.number_input("回看根数 lookback_bars", min_value=3, max_value=50, value=6, step=1)
    with colB:
        must_be_in_last_n = st.number_input("形态必须出现在最近 N 根", min_value=1, max_value=5, value=2, step=1)
        volume_multiplier = st.number_input("放量倍数阈值", min_value=0.5, max_value=10.0, value=1.0, step=0.1)
        display_limit = st.number_input("展示数量", min_value=1, max_value=200, value=50, step=1)
    with colC:
        cache_ttl = st.number_input("缓存 TTL(秒)", min_value=30, max_value=1200, value=240, step=30)
        refresh_sec = st.number_input("刷新间隔(秒)", min_value=2, max_value=300, value=120, step=5)

    st.caption(
        "说明：扫描会遍历可交易的 USDT 永续合约，拉取最近 lookback_bars 根 K 线，"
        "只允许形态出现在最后 N 根，并做放量与趋势过滤。缓存 TTL 用于避免频繁请求导致限流。"
    )

    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
    else:
        do_scan = bool(manual_scan_once)
        # ❌ 锤子线扫描页不再使用 autorefresh 触发扫描
        # 页面刷新交给“全局刷新”即可
        pass

        if do_scan:
            try:
                # ✅ 永远使用“同一份K线数据”同时计算：锤子线 + 双实体K线（不再分开启动）
                combo_data = exchange.scan_hammer_and_overlap_pairs_usdt(
                    interval=str(interval),
                    hammer_lookback_bars=int(lookback_bars),
                    hammer_must_be_in_last_n=int(must_be_in_last_n),
                    hammer_volume_multiplier=float(volume_multiplier),
                    overlap_ratio=float(st.session_state.get("overlap_ratio", 80.0)) / 100.0,
                    vol_boost=float(st.session_state.get("vol_boost", 1.30)),
                    cache_ttl=int(cache_ttl),
                ) or {"hammer": [], "overlap": []}

                # 缓存 combo 结果，给下面“双K重叠扫描”复用
                st.session_state["_combo_scan_data"] = combo_data
                st.session_state["_combo_scan_interval"] = str(interval)

                rows = combo_data.get("hammer") or []


                if display_limit:
                    rows = rows[: int(display_limit)]

                st.session_state["_hammer_rows_cache"] = rows
                st.session_state["_hammer_rows_cache_ts"] = float(time.time())

                if not rows:
                    st.warning("本次扫描没有命中符合条件的锤子线/倒锤子线。可尝试：降低放量倍数阈值、增大回看根数、或切换周期。")
                else:
                    import pandas as pd

                    df = pd.DataFrame(rows)
                    # 统一展示字段
                    if "symbol" in df.columns:
                        df["symbol"] = df["symbol"].astype(str).str.upper().str.replace("/", "", regex=False)

                    column_map = {
                        "symbol": "交易对",
                        "mode": "建议方向",
                        "pattern": "形态",
                        "pinbar_index": "出现位置",
                        "hammer_score": "形态强度",
                        "volume_ratio": "放量倍数",
                        "same_dir_k_count": "同向K数量(近6)",
                        "extreme_dist": "极值距离(近6)",
                        "extreme_dist_ratio": "极值/锤长",
                        "hammer_len": "锤子线长度",
                        "extreme_type": "极值类型",
                        "priority": "优先",
                        "slope": "趋势斜率",
                    }
                    df_show = df.rename(columns=column_map)

                    if "建议方向" in df_show.columns:
                        df_show["建议方向"] = df_show["建议方向"].map({
                            "short": "做空",
                            "long": "做多",
                        }).fillna(df_show["建议方向"])

                    # 选择列
                    if "✅选择" not in df_show.columns:
                        df_show.insert(0, "✅选择", False)

                    edited = st.data_editor(
                        df_show,
                        use_container_width=True,
                        height=460,
                        hide_index=True,
                        column_config={"✅选择": st.column_config.CheckboxColumn(required=False)},
                    )

                    picked = edited[edited["✅选择"] == True]
                    colP1, colP2 = st.columns([1, 2])
                    with colP1:
                        if st.button("➡️ 使用选中交易对", disabled=picked.empty):
                            sym = str(picked.iloc[0]["交易对"]).strip().upper().replace("/", "")
                            st.session_state["selected_symbol"] = sym
                            st.success(f"已选择：{sym}（已同步到下单面板）")
                    with colP2:
                        st.caption("勾选一行后点按钮，会把交易对同步到下单面板的输入框。")

            except Exception as e:
                st.error(f"扫描失败：{e}")
                st.exception(e)
        else:
            # 扫描暂停：不打 REST，仅展示缓存（如有）
            rows = st.session_state.get("_hammer_rows_cache") or []
            ts = st.session_state.get("_hammer_rows_cache_ts")
            if rows:
                if ts:
                    st.info(f"扫描已暂停：当前展示缓存结果（上次扫描：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))}）")
                else:
                    st.info("扫描已暂停：当前展示缓存结果（无时间戳）")
            else:
                st.info("扫描已暂停：暂无缓存结果。你可以点一次“手动扫描一次”。")
            
    st.divider()
    st.subheader("扫描：双K实体80%重叠 + 近两根放量(>= 前四根均量 * 1.30)")

    # ✅ 不再单独扫描；复用上面“手动扫描一次”的 combo 结果（同一次K线数据）
    oc1, oc2, oc3 = st.columns([1, 1, 1])
    with oc1:
        st.caption("提示：请在上方点击「🔍 手动扫描一次」，本区域会自动展示同周期的双K结果。")
    with oc2:
        overlap_ratio = st.number_input(
            "实体重叠阈值(长实体%)",
            min_value=50.0, max_value=100.0, value=80.0, step=1.0,
            key="overlap_ratio",
        )
        vol_boost = st.number_input(
            "放量阈值(倍数)",
            min_value=1.0, max_value=10.0, value=1.30, step=0.05,
            key="vol_boost",
        )
    with oc3:
        overlap_display_limit = st.number_input(
            "展示数量(重叠扫描)",
            min_value=1, max_value=200, value=50, step=1,
            key="overlap_display_limit",
        )

    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
    else:
        combo_data = st.session_state.get("_combo_scan_data") or {}
        combo_interval = str(st.session_state.get("_combo_scan_interval", ""))

        # 只展示“与上方锤子线扫描同周期”的双K结果
        current_interval = str(interval)  # 与上方选择的K线周期一致
        if combo_data and combo_interval == current_interval:
            rows2 = (combo_data.get("overlap") or [])[: int(overlap_display_limit)]
            if not rows2:
                st.warning("当前周期的双K扫描结果为空。可尝试：降低重叠阈值/放量阈值，然后再点一次上方「🔍 手动扫描一次」。")
            else:
                df2 = pd.DataFrame(rows2)
                df2["symbol"] = df2["symbol"].astype(str).str.upper().str.replace("/", "", regex=False)
                df2 = df2.rename(columns={
                    "symbol": "交易对",
                    "overlap_ratio": "实体重叠比例(长实体)",
                    "vol_ratio": "放量倍数(近2/前4)",
                    "last2_avg_vol": "近2均量",
                    "prev4_avg_vol": "前4均量",
                })
                if "✅选择" not in df2.columns:
                    df2.insert(0, "✅选择", False)

                edited2 = st.data_editor(
                    df2,
                    use_container_width=True,
                    height=460,
                    hide_index=True,
                    column_config={"✅选择": st.column_config.CheckboxColumn(required=False)},
                )
                picked2 = edited2[edited2["✅选择"] == True]
                if st.button("➡️ 使用选中交易对(重叠扫描)", disabled=picked2.empty, key="use_overlap_pick"):
                    sym = str(picked2.iloc[0]["交易对"]).strip().upper().replace("/", "")
                    st.session_state["selected_symbol"] = sym
                    st.success(f"已选择：{sym}（已同步到下单面板）")
        else:
            st.info("暂无可展示的双K结果：请先在上方选择相同周期，并点击一次「🔍 手动扫描一次」。")


# ========== Page: Ladder + Manual Order ==========
elif page == "🧩 阶梯 + 手动下单":
    st.subheader("手动交易面板：阶梯策略 + 手动下单（Hedge / LONG+SHORT）")

    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
    else:
        # 以前这里有“页面自动刷新”用来强制 rerun 以消费 WS 队列。
        # 现在 user stream 直接在 WS 回调里分发给 bot，不再需要刷新开关。
        
        # 初始化 bot
        if "ladder_bot" not in st.session_state:
            st.session_state["ladder_bot"] = LadderBot(exchange, logger)
        bot: LadderBot = st.session_state["ladder_bot"]
        bot.exchange = exchange

        # 读取扫描页选择的 symbol
        default_symbol = st.session_state.get("selected_symbol", bot.cfg.symbol)

        st.markdown("### 🧩 阶梯下单（价格触发即下单，第一单限价）")
        c1, c2, c3, c4 = st.columns([1.2, 1.0, 1.0, 1.0])
        with c1:
            symbol = st.text_input("交易对（USDT 永续）", value=default_symbol)
            side = st.selectbox("方向（阶梯）", options=["short", "long"], index=0 if bot.cfg.side == "short" else 1)
        with c2:
            base_qty = st.number_input("每次下单数量", min_value=0.0001, value=float(bot.cfg.base_qty), step=0.001, format="%.6f")
            step_pct = st.number_input("步长 %（5=每次*1.05 或 *0.95）", min_value=0.1, max_value=200.0, value=float(bot.cfg.step_pct * 100.0), step=0.1)
        with c3:
            limit_offset = st.number_input("限价偏移 %（0.1=0.1%）", min_value=0.0, max_value=5.0, value=float(bot.cfg.limit_offset_pct * 100.0), step=0.05)
            tick_interval = st.number_input("轮询间隔(秒)", min_value=0.2, max_value=10.0, value=float(bot.cfg.tick_interval_sec), step=0.2)
        with c4:
            enable_ladder = st.toggle("启用阶梯加仓", value=bool(bot.cfg.enable_ladder))
            enable_tp_reset = st.toggle("阶梯后重设 TP", value=bool(bot.cfg.enable_tp_reset))
            tp_pct = st.number_input("阶梯TP %（0.2=0.2%）", min_value=0.01, max_value=50.0, value=float(bot.cfg.tp_pct * 100.0), step=0.05)

        cfg = LadderConfig(
            symbol=symbol.strip().upper().replace("/", ""),
            side=side,
            base_qty=float(base_qty),
            step_pct=float(step_pct) / 100.0,
            limit_offset_pct=float(limit_offset) / 100.0,
            tick_interval_sec=float(tick_interval),
            enable_ladder=bool(enable_ladder),
            enable_tp_reset=bool(enable_tp_reset),
            tp_pct=float(tp_pct) / 100.0,
        )

        colS1, colS2, colS3 = st.columns([1, 1, 2])
        with colS1:
            if st.button("✅ 应用阶梯参数"):
                bot.configure(cfg)
                st.success("已应用")
        with colS2:
            if st.button("🚀 启动阶梯"):
                bot.configure(cfg)
                bot.start()
        with colS3:
            if st.button("🛑 停止阶梯"):
                bot.stop()
                
        st.divider()
        st.markdown("### 🎯 区间两单策略（A1/A2 + 固定TP/SL + 条件补挂保本止损）")

        # 初始化 RangeTwoBot
        if "range2_bot" not in st.session_state:
            st.session_state["range2_bot"] = RangeTwoBot(exchange, logger)
        rbot: RangeTwoBot = st.session_state["range2_bot"]
        rbot.exchange = exchange
        # 注册到 WS 回调分发器：A1/A2 成交事件会在 WS 线程里直接触发 SL/TP 挂单
        register_range2_bot(rbot)

        rc1, rc2, rc3, rc4 = st.columns([1.2, 1.0, 1.0, 1.0])
        with rc1:
            r_symbol = st.text_input("交易对（区间两单）", value=default_symbol, key="r2_symbol")
            r_side = st.selectbox("方向（区间两单）", options=["long", "short"], index=0, key="r2_side")
        with rc2:
            r_qty1 = st.number_input("A1 数量", min_value=0.0001, value=float(rbot.cfg.qty1), step=0.001, format="%.6f", key="r2_qty1")
            r_qty2 = st.number_input("A2 数量", min_value=0.0001, value=float(rbot.cfg.qty2), step=0.001, format="%.6f", key="r2_qty2")
        with rc3:
            r_p1 = st.number_input("价格输入1", min_value=0.0, value=float(rbot.cfg.price_a), step=0.01, format="%.6f", key="r2_p1")
            r_p2 = st.number_input("价格输入2", min_value=0.0, value=float(rbot.cfg.price_b), step=0.01, format="%.6f", key="r2_p2")
        with rc4:
            r_off2 = st.number_input("A2 距离当前价 %（1=1%）", min_value=0.0, max_value=50.0, value=float(rbot.cfg.second_entry_offset_pct * 100.0), step=0.1, key="r2_off2")
            r_be = st.number_input("保本止损偏移 %（0.1=0.1%）", min_value=0.01, max_value=5.0, value=float(rbot.cfg.be_offset_pct * 100.0), step=0.01, key="r2_be")

        r_tick = st.number_input("监控间隔(秒)", min_value=0.2, max_value=10.0, value=float(rbot.cfg.tick_interval_sec), step=0.2, key="r2_tick")

        # 预览计算（不阻止）
        try:
            lo, hi = (float(r_p1), float(r_p2)) if float(r_p1) <= float(r_p2) else (float(r_p2), float(r_p1))
            d = hi - lo
            if d > 0:
                if r_side == "long":
                    prev_sl, prev_tp = lo, hi + d / 2.0
                else:
                    prev_sl, prev_tp = hi, lo - d / 2.0
                prev_a1 = (lo + hi) / 2.0
                st.caption(f"预览：A1≈{prev_a1:.6f} | SL≈{prev_sl:.6f} | TP≈{prev_tp:.6f}（A2=当前价±{float(r_off2):.2f}%）")
        except Exception:
            pass

        rcol1, rcol2, rcol3, rcol4 = st.columns([1, 1, 1, 2])
        with rcol1:
            if st.button("✅ 应用区间参数", key="r2_apply"):
                rcfg = RangeTwoConfig(
                    symbol=str(r_symbol).strip().upper().replace("/", ""),
                    side=str(r_side),
                    qty1=float(r_qty1),
                    qty2=float(r_qty2),
                    price_a=float(r_p1),
                    price_b=float(r_p2),
                    second_entry_offset_pct=float(r_off2) / 100.0,
                    be_offset_pct=float(r_be) / 100.0,
                    tick_interval_sec=float(r_tick),
                )
                rbot.configure(rcfg)
                st.success("已应用")
        with rcol2:
            if st.button("📤 下 A1/A2", key="r2_place"):
                try:
                    rbot.reset_runtime_flags()
                    rbot.place_initial_orders()
                    # ✅ 最干净的做法：下完单就自动启动监控
                    rbot.start()
                    st.success("已发送 A1/A2，并已自动启动监控（回执见日志）")
                except Exception as e:
                    st.error(f"下单失败：{e}")
                    logger.log(f"❌ 区间两单下单失败：{e}")
        with rcol3:
            if st.button("🚀 启动监控", key="r2_start"):
                rbot.start()
        with rcol4:
            if st.button("🛑 停止监控", key="r2_stop"):
                rbot.stop()

        st.caption("说明：A1/A2 都会自动带固定 TP/SL；监控只负责在满足条件后补挂 closePosition 的 STOP_MARKET（全部止损）。建议先 dry_run 测试。")
        st.json(asdict(rbot.state), expanded=False)

        st.divider()
        st.markdown("### 🧾 手动下单（市价/限价 + 可选 TP/SL，价格输入）")

        order_symbol = st.session_state.get("selected_symbol", bot.cfg.symbol)

        o1, o2, o3, o4 = st.columns([1.2, 1.0, 1.0, 1.0])
        with o1:
            sym2 = st.text_input("交易对（用于手动下单）", value=order_symbol)
        with o2:
            order_side = st.selectbox("方向（手动下单）", options=["long", "short"], index=0)
        with o3:
            order_type = st.selectbox("订单类型", options=["market", "limit"], index=0)
        with o4:
            qty = st.number_input("数量", min_value=0.0001, value=0.001, step=0.001, format="%.6f")

        price = None
        if order_type == "limit":
            price = st.number_input("限价价格", min_value=0.0, value=0.0, step=0.01, format="%.6f")

        # 当前价（仅参考提示）
        mark = None
        try:
            t = exchange.get_ticker(sym2) or {}
            mark = float(t.get("lastPrice") or t.get("markPrice") or 0.0) or None
        except Exception:
            mark = None
        st.caption(f"当前价(参考)：{mark}" if mark else "当前价：获取失败（不影响你手动输入价格下单）")

        # 总开关：只下主单 / 自动挂保护单
        auto_protection = st.toggle("自动挂保护单（TP/SL）", value=False)
        st.caption("关闭时：只下主单；开启时：主单下完后会自动创建 TP/SL 子单。")

        pcol1, pcol2, pcol3 = st.columns([1, 1, 2])
        with pcol1:
            enable_tp = st.toggle("启用止盈(TP)", value=False, disabled=not auto_protection)
        with pcol2:
            enable_sl = st.toggle("启用止损(SL)", value=False, disabled=not auto_protection)
        with pcol3:
            st.caption("Hedge 模式下将按 positionSide=LONG/SHORT 绑定保护单。")

        tp_price = sl_price = None
        tp_col, sl_col = st.columns([1, 1])
        with tp_col:
            if enable_tp:
                tp_price = st.number_input("TP 触发价（直接输入价格）", min_value=0.0, value=0.0, step=0.01, format="%.6f")
        with sl_col:
            if enable_sl:
                sl_price = st.number_input("SL 触发价（直接输入价格）", min_value=0.0, value=0.0, step=0.01, format="%.6f")

        # 方向提示（不阻止下单）
        if mark and auto_protection:
            if order_side == "long":
                if enable_tp and tp_price and tp_price > 0 and tp_price <= mark:
                    st.warning("⚠️ 多单 TP 通常高于当前价（你输入的 TP ≤ 当前价）")
                if enable_sl and sl_price and sl_price > 0 and sl_price >= mark:
                    st.warning("⚠️ 多单 SL 通常低于当前价（你输入的 SL ≥ 当前价）")
            else:
                if enable_tp and tp_price and tp_price > 0 and tp_price >= mark:
                    st.warning("⚠️ 空单 TP 通常低于当前价（你输入的 TP ≥ 当前价）")
                if enable_sl and sl_price and sl_price > 0 and sl_price <= mark:
                    st.warning("⚠️ 空单 SL 通常高于当前价（你输入的 SL ≤ 当前价）")

        colO1, colO2 = st.columns([1, 2])
        with colO1:
            if st.button("📤 发送订单"):
                try:
                    position_side = "LONG" if order_side == "long" else "SHORT"
                    params = {
                        "timeInForce": "GTC",
                        "tag": "MANUAL_UI",
                        "positionSide": position_side,   # ✅ Hedge 关键
                    }

                    # ✅ 只有总开关开启时才附带 TP/SL
                    if auto_protection:
                        if enable_tp and tp_price and tp_price > 0:
                            params["take_profit"] = {"price": float(tp_price)}
                        if enable_sl and sl_price and sl_price > 0:
                            params["stop_loss"] = {"price": float(sl_price)}

                    o = exchange.create_order(
                        symbol=sym2.strip().upper().replace("/", ""),
                        side=order_side,            # long/short（交给封装转换）
                        order_type=order_type,      # market/limit
                        quantity=float(qty),
                        price=float(price) if (order_type == "limit" and price and price > 0) else None,
                        params=params,
                    )
                    logger.log(f"✅ 手动下单成功：{o}")
                    st.success("下单已发送（回执见日志）")
                except Exception as e:
                    st.error(f"下单失败：{e}")
                    logger.log(f"❌ 手动下单失败：{e}")

        with colO2:
            st.caption("建议先勾选 dry_run 测试；实盘前务必确认：合约类型、最小下单量、杠杆、保证金模式、Hedge 模式。")

        st.divider()
        st.markdown("### 📌 当前仓位（Hedge：LONG + SHORT）")

        try:
            pos = exchange._get_ws_positions(sym2.strip().upper().replace("/", "")) or []
            rows = []
            for p in pos if isinstance(pos, list) else [pos]:
                if not isinstance(p, dict):
                    continue
                if str(p.get("symbol", "")).replace("/", "") != str(sym2).replace("/", ""):
                    continue
                rows.append({
                    "交易对": p.get("symbol"),
                    "方向(positionSide)": p.get("positionSide"),
                    "数量(positionAmt)": p.get("positionAmt"),
                    "开仓均价(entryPrice)": p.get("entryPrice"),
                    "未实现盈亏(UPnL)": p.get("unrealizedProfit"),
                    "强平价(liqPrice)": p.get("liquidationPrice"),
                    "杠杆(leverage)": p.get("leverage"),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
        except Exception as e:
            st.warning(f"获取仓位失败：{e}")

        st.divider()
        st.markdown("### 🧷 阶梯运行状态")
        st.json(asdict(bot.state), expanded=True)


# ========== Page: Logs ==========
elif page == "🧾 日志":
    st.subheader("实时日志（最近 300 行）")
    st.code(logger.tail(300), language="text")

elif page == "📊 账户":
    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
    else:
        symbol_filter = st.text_input("symbol 过滤（可空）", value=st.session_state.get("selected_symbol", "")).strip().upper().replace("/", "")
        symbol_filter = symbol_filter or None
        render_account_panel(exchange, symbol=symbol_filter)
