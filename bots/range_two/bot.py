# -*- coding: utf-8 -*-
"""
RangeTwo bot (config/state/bot) extracted from app/streamlit_app.py (commit #4 split).
Keeping behavior unchanged; only moved out for maintainability.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, List

import streamlit as st  # RangeTwo code references session_state in a few places (kept for compatibility)

from bots.base import BotBase
from bots.common.ticker_mixin import TickerSubscriptionMixin
from bots.range_two.logic import (
    normalize_range as range2_normalize_range,
    plan_orders as range2_plan_orders,
    calc_current_bep as range2_calc_current_bep,
)
from infra.logging.ui_logger import UILogger

try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange  # type: ignore


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


class RangeTwoBot(BotBase, TickerSubscriptionMixin):
    def __init__(self, exchange: "BinanceExchange", ui_logger: "UILogger"):
        super().__init__()
        BotBase.__init__(self, name=\"RangeTwoBot\")
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

        lo, hi, d = range2_normalize_range(float(cfg.price_a), float(cfg.price_b))
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
        # 注意：RangeTwoBot 的 MRO 是 RangeTwoBot -> BotBase -> TickerSubscriptionMixin
        # super() 会先到 BotBase（它没有 _ensure_ticker_ws），所以这里必须显式调用 mixin。
        TickerSubscriptionMixin._ensure_ticker_ws(self, self.exchange, symbol, factory, on_log=self.log.log)

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


        sl, tp, a1_price, a2_price = range2_plan_orders(
            side=str(cfg.side),
            low=float(lo),
            high=float(hi),
            diff=float(d),
            mark=float(mark),
            second_entry_offset_pct=float(cfg.second_entry_offset_pct),
        )

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
        s = self.state
        return range2_calc_current_bep(
            a1_filled=bool(s.a1_filled),
            a2_filled=bool(s.a2_filled),
            a1_entry_price=s.a1_entry_price,
            a2_entry_price=s.a2_entry_price,
            a1_limit_price=s.a1_limit_price,
            a2_limit_price=s.a2_limit_price,
            qty1=float(cfg.qty1 or 0.0),
            qty2=float(cfg.qty2 or 0.0),
        )

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

        st.caption("两段式 StopLimit（待触发）")
        try:
            pdsl = ex.get_pending_deferred_stop_limits()
        except Exception:
            pdsl = []
        st.dataframe(pd.DataFrame(pdsl), use_container_width=True, height=240)


