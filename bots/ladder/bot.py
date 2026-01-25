# -*- coding: utf-8 -*-
"""
Ladder bot (config/state/bot) extracted from app/streamlit_app.py (commit #4 split).
Keeping behavior unchanged; only moved out for maintainability.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

from bots.base import BotBase
from bots.common.ticker_mixin import TickerSubscriptionMixin
from bots.ladder.logic import (
    calc_next_add_price as ladder_calc_next_add_price,
    entry_limit_price as ladder_entry_limit_price,
    should_add as ladder_should_add,
)
from infra.logging.ui_logger import UILogger

# Import exchange type for hints / usage
try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange  # type: ignore


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


class LadderBot(BotBase, TickerSubscriptionMixin):
    def __init__(self, exchange: BinanceExchange, ui_logger: UILogger):
        super().__init__()
        BotBase.__init__(self, name="LadderBot")
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
        # 注意：LadderBot 的 MRO 是 LadderBot -> BotBase -> TickerSubscriptionMixin
        # super() 会先到 BotBase（它没有 _ensure_ticker_ws），所以这里必须显式调用 mixin。
        TickerSubscriptionMixin._ensure_ticker_ws(self, self.exchange, symbol, factory, on_log=self.log.log)


    def _calc_next_add_price(self, last_entry: float, side: str, step: float) -> float:
        return float(ladder_calc_next_add_price(float(last_entry), str(side), float(step)))

    def _entry_limit_price(self, mark: float, side: str, offset: float) -> float:
        return float(ladder_entry_limit_price(float(mark), str(side), float(offset)))

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
                    should_add = ladder_should_add(float(mark), self.state.next_add_price, str(cfg.side))
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




