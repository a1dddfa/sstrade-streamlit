# -*- coding: utf-8 -*-
"""
ETHUSDC 交易策略（修正版）
================================
核心特性：
1. 启动时获取当前市价，记为价格1（price_1）
2. 在价格1上方放 A1 做多触发单，在下方放 A2 做空触发单（均为 STOP_LIMIT 限价触发）
3. 每个入场单都自动挂对应的止盈 / 止损：
   - A1：触发价 price_1 * 1.001，止盈 price_1 * 1.006，止损回到 price_1 * 0.999
   - A2：触发价 price_1 * 0.999，止盈 price_1 * 0.994，止损回到 price_1 * 1.001
4. 止损触发：仅重建对应方向的 A1 或 A2（保持原来的 price_1 不变）
5. 止盈触发：整个策略重置，重新取当前市价为新的价格1，并重新挂 A1/A2
6. 防抖 & 安全逻辑：
   - 使用冷却时间，避免瞬间重复下单
   - 有持仓期间不会重复创建新的 A1/A2
   - 通过 TP/SL + _reset_strategy 控制完整一轮交易的生命周期
"""
import logging
import time
import math
import re
import threading
from typing import Dict, Any, Optional
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


# ===== 策略参数统一配置 =====
class GridParams:
    # 交易对
    DEFAULT_SYMBOL = "ETHUSDC"

    # 杠杆 & 资金使用
    LEVERAGE = 1
    FUNDS_RATIO = 0.001          # 使用资产的 0.1%

    # A1 / A2 进场偏移（相对 price_1）
    A1_ENTRY_OFFSET = -0.001     # +0.05%
    A2_ENTRY_OFFSET = 0.001     # -0.05%

    # 止盈偏移（相对 price_1）
    A1_TP_OFFSET = -0.00         # +0.15%
    A2_TP_OFFSET = 0.00       # -0.15%

    # 止损触发（相对 price_1）
    A1_SL_TRIGGER_OFFSET = -0.03   # 回到 0.999 * price_1
    A2_SL_TRIGGER_OFFSET = 0.03    # 回到 1.001 * price_1

    # 止损限价相对触发价的价差（多一点保证成交）
    A1_SL_LIMIT_FACTOR = 0.999   # 多头止损限价 = trigger * 0.999
    A2_SL_LIMIT_FACTOR = 1.001   # 空头止损限价 = trigger * 1.001

    # 是否使用交易所原生 STOP-LIMIT 作为止损（入场成交后立即挂 SL）
    USE_EXCHANGE_STOP_LOSS = False

    # 下单数量相关
    MIN_AMOUNT = 0.002
    AMOUNT_STEP = 0.001
    MIN_NOTIONAL = 20.0

    # 冷却时间
    ORDER_COOLDOWN_SECONDS = 2.0


    # ===== 新增：止损后波动率冷却机制 =====
    # 止损后先暂停运行 1 小时；之后每小时检测【前 1 小时振幅】(high-low)/low
    # 若振幅 > 3%，继续暂停 1 小时；直到振幅 <= 3% 再恢复策略循环
    SL_PAUSE_SECONDS = 3600
    VOLATILITY_CHECK_THRESHOLD = 0.03   # 3%
    VOL_CHECK_TIMEFRAME = "1m"          # 取 1 分钟 K 线做近 60 根的振幅估算
    VOL_CHECK_LIMIT = 60
    # 新增：网格级别标识
    LEVELS = ["A", "B", "C", "D", "E", "F","G", "H"]

    # 新增：多头方向每一级的默认配置
    # 说明：
    #   enabled      是否启用这一层
    #   entry_offset 入场价 = price_1 * (1 + entry_offset)
    #   tp_offset    止盈价 = price_1 * (1 + tp_offset)   （这里只是给你参考，如果以后要做分级 TP）
    #   size_factor  数量 = base_quantity * size_factor
    #   fixed_qty    如果填了固定数量，则优先用 fixed_qty，忽略 size_factor
    LONG_LADDER_DEFAULT = {
        "A": {"enabled": True, "entry_offset": -0.00125, "tp_offset": 0.00, "size_factor": 1.0, "fixed_qty": None},
        "B": {"enabled": True, "entry_offset": -0.0025, "tp_offset": -0.00125, "size_factor": 1.0, "fixed_qty": None},
        "C": {"enabled": True, "entry_offset": -0.005, "tp_offset": -0.0025, "size_factor": 1.0, "fixed_qty": None},
        "D": {"enabled": True, "entry_offset": -0.01, "tp_offset": -0.005, "size_factor": 3.0, "fixed_qty": None},
        "E": {"enabled": True, "entry_offset": -0.02, "tp_offset": -0.01, "size_factor": 6.0, "fixed_qty": None},
        "F": {"enabled": False, "entry_offset": -0.04, "tp_offset": -0.03, "size_factor": 12.0, "fixed_qty": None},
        "G": {"enabled": False, "entry_offset": -0.06, "tp_offset": -0.05, "size_factor": 24.0, "fixed_qty": None},
        "H": {"enabled": False, "entry_offset": -0.08, "tp_offset": -0.06, "size_factor": 48.0, "fixed_qty": None},
    }

    # 新增：空头方向每一级的默认配置
    SHORT_LADDER_DEFAULT = {
        "A": {"enabled": True, "entry_offset": 0.00125, "tp_offset": 0.00, "size_factor": 1.0, "fixed_qty": None},
        "B": {"enabled": True, "entry_offset": 0.0025, "tp_offset": 0.00125, "size_factor": 1.0, "fixed_qty": None},
        "C": {"enabled": True, "entry_offset": 0.005, "tp_offset": 0.0025, "size_factor": 1.0, "fixed_qty": None},
        "D": {"enabled": True, "entry_offset": 0.01, "tp_offset": 0.005, "size_factor": 3.0, "fixed_qty": None},
        "E": {"enabled": True, "entry_offset": 0.02, "tp_offset": 0.01, "size_factor": 6.0, "fixed_qty": None},
        "F": {"enabled": False, "entry_offset": 0.04, "tp_offset": 0.03, "size_factor": 12.0, "fixed_qty": None},
        "G": {"enabled": False, "entry_offset": 0.06, "tp_offset": 0.05, "size_factor": 24.0, "fixed_qty": None},
        "H": {"enabled": False, "entry_offset": 0.08, "tp_offset": 0.06, "size_factor": 48.0, "fixed_qty": None},
    }

class SymbolTracker:
    """交易对跟踪器 - 跟踪价格1与 A1/A2 主触发单"""

    def __init__(self,symbol):
        self.symbol = symbol
        self.price_1: float = 0.0
        
        # 改成所有级别
        self.active_orders = {
            "long":  {level: None for level in GridParams.LEVELS},
            "short": {level: None for level in GridParams.LEVELS},
        }
        self.last_order_time: float = 0.0

    # ===== 统一 ID 处理 =====
    @staticmethod
    def get_canonical_id_from_order(order: Dict[str, Any]) -> Optional[str]:
        """统一从订单对象中提取可用 ID：优先 clientOrderId，然后 id，再然后 orderId"""
        if not order:
            return None
        return (
            order.get("clientOrderId")
            or order.get("id")
            or order.get("orderId")
            or order.get("canonical_id")
        )

    def has_active_order(self, side: str, order_type: str) -> bool:
        """检查是否有指定方向 + 类型的活跃订单"""
        return bool(self.active_orders.get(side, {}).get(order_type))

    def set_active_order(self, side: str, order_type: str, order: Dict[str, Any]) -> None:
        """在 tracker 中记录一个激活的订单（按 canonical_id）"""
        cid = self.get_canonical_id_from_order(order)
        if cid:
            self.active_orders.setdefault(side, {})[order_type] = str(cid)

    def clear_active_order(self, side: str, order_type: str) -> None:
        """清除指定方向+类型的订单跟踪"""
        if side in self.active_orders and order_type in self.active_orders[side]:
            self.active_orders[side][order_type] = None

    # ===== 冷却控制 =====
    def update_last_order_time(self) -> None:
        self.last_order_time = time.time()

    def is_cooling_down(self, cooldown_seconds: float) -> bool:
        return time.time() - self.last_order_time < cooldown_seconds

    # ===== 与交易所 open_orders 同步 =====
    def cleanup_completed_orders(self, open_orders: list, logger: logging.Logger) -> None:
        """根据交易所返回的 open_orders 清理已经完成/取消的 A1/A2 主单"""
        # ⚠️ 保险：如果 open_orders 是 None，说明请求失败，直接跳过，避免误删
        if open_orders is None:
            logger.warning("⚠️ open_orders 为 None，跳过清理，避免误删本地订单状态")
            return

        still_open_ids = set()
        for o in open_orders or []:
            cid = (
                o.get("clientOrderId")
                or o.get("id")
                or o.get("orderId")
            )
            if cid:
                still_open_ids.add(str(cid))

        for side in ["long", "short"]:
            for level in GridParams.LEVELS:
                cur_id = self.active_orders.get(side, {}).get(level)
                if not cur_id:
                    continue
                if str(cur_id) not in still_open_ids:
                    logger.info(
                        f"🧹 {side} 方向的 {level} 单({cur_id}) 已不在未成交列表中，视为已完成/取消，从跟踪器移除"
                    )
                    self.active_orders[side][level] = None


class EthUSDCGridStrategy(BaseStrategy):
    """ETHUSDC 网格策略（A1/A2 单边循环 + 止盈/止损）"""

    # 订单冷却时间（秒）
    ORDER_COOLDOWN_SECONDS = GridParams.ORDER_COOLDOWN_SECONDS

    def __init__(self, config: Dict, exchange, order_manager, risk_manager, data_processor):
        self.symbol_trackers: Dict[str, SymbolTracker] = {}
        self.symbol = config.get("symbol", GridParams.DEFAULT_SYMBOL)
        self.pair = self.symbol
        self._request_remove_pair = False
        self._request_remove_pair_reason = ""

        # 交易方向控制：'both' / 'long_only' / 'short_only'
        self.trade_mode = (config.get('trade_mode') or 'both').lower()
        # ===== 动态反向模式状态（仅在 dynamic_reversal_mode=True 且 trade_mode=short_only/long_only 时启用）=====
        self._dyn_anchor_price = None  # 24h open 或推导出的“起涨/起跌价”
        self._dyn_last_entry_price = None
        self._dyn_next_add_price = None
        self._dyn_fixed_qty = None
        self._dyn_tp_order_id = None
        self._dyn_sl_order_id = None
        self._dyn_step_pct = float(config.get("ladder_step_pct", 0.075))
        self.limit_offset_pct = float(config.get("limit_offset_pct", 0.001))  # 0.1% 偏移下限价单
        self._dyn_liq_buffer = float(config.get("stop_loss_liq_buffer", 0.10))  # 距离强平价的缓冲（10%）

        self._entry_filled_seen_lock = threading.Lock()
        self._entry_filled_seen_ids = set()
        
        # ===== 新增：以 ENTRY_FILLED 作为权威持仓状态（防止 positions 延迟导致重复开仓）=====
        self._authoritative_pos_open = {"long": False, "short": False}

        # ===== 新增：幂等杠杆设置（避免每个 tick 都打 REST）=====
        self._leverage_set: bool = False
        self._leverage_last_try_ts: float = 0.0  # 上次尝试 set_leverage 的时间戳

        # ===== 新增：行情 WebSocket（用来更新交易所缓存，避免 REST 轮询）=====
        self._ws_ticker_started: bool = False

        # ===== 新增：止损后暂停/波动率检测状态 =====
        self._paused_until_ts: float = 0.0
        self._pause_reason: str = ""
        self._resume_needs_reset: bool = False

        # ===== 新增：手动止损限价单 pending 状态（等待成交后再进入暂停）=====
        self._manual_sl_pending: bool = False
        self._manual_sl_pending_tag: Optional[str] = None
        self._manual_sl_pending_order_id: Optional[str] = None


        # 新增：构建阶梯配置（可以被外部 config 覆盖）
        self.ladder_config = self._build_ladder_config({})
        
        super().__init__(config, exchange, order_manager, risk_manager, data_processor)

    def _build_ladder_config(self, overrides: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """
        生成最终的阶梯配置：
        - 先用 GridParams 的默认值
        - 再用外部 overrides 覆盖，例如：
          {
            "long": {
              "B": {"enabled": True, "entry_offset": -0.003, "size_factor": 2.0},
              "C": {"enabled": True, "fixed_qty": 0.3},
            },
            "short": {
              "B": {"enabled": True, "entry_offset": 0.003, "size_factor": 2.0},
            }
          }
        """
        cfg = {
            "long":  {lvl: dict(GridParams.LONG_LADDER_DEFAULT[lvl])  for lvl in GridParams.LEVELS},
            "short": {lvl: dict(GridParams.SHORT_LADDER_DEFAULT[lvl]) for lvl in GridParams.LEVELS},
        }

        for side_key in ["long", "short"]:
            side_override = overrides.get(side_key) or {}
            for level, lv_conf in side_override.items():
                if level not in GridParams.LEVELS:
                    continue
                cfg[side_key][level].update(lv_conf or {})

        return cfg

    # ================= 初始化 =================

    def _initialize(self):
        """初始化策略"""
        super()._initialize()
        self.logger.info(f"初始化 {self.symbol} 策略")
        self.symbol_trackers[self.symbol] = SymbolTracker(self.symbol)
        self._init_level_1_orders()
        # ✅ 只轮询：如果全局禁用 WS，就不要订阅（行情由 TickerWatchPoller 每分钟更新缓存提供）
        use_ws = bool((getattr(self.exchange, "global_config", {}) or {}).get("use_ws", False))
        if use_ws and (not self._ws_ticker_started):
            try:
                self.exchange.ws_subscribe_ticker(self.pair, self._on_ws_ticker)
                self._ws_ticker_started = True
            except Exception as e:
                self.logger.error(f"❌ 订阅行情 WebSocket 失败，将回退到 REST: {e}", exc_info=True)


    def _init_level_1_orders(self) -> None:
        """初始化价格1（price_1）"""
        self.logger.info(f"开始初始化 {self.symbol} 价格1")
        try:
            ticker = self.exchange.get_ticker(self.symbol)
            price_1 = float(ticker.get("lastPrice", 0.0))
            if price_1 <= 0:
                raise ValueError(f"获取到的价格1无效: {price_1}")

            tracker = self.symbol_trackers[self.symbol]
            tracker.price_1 = price_1

            # 也存到策略状态（如果你有可视化/恢复逻辑可以用到）
            self.update_state({f"{self.pair}_price_1": price_1})

            self.logger.info(f"✅ 初始化成功 - {self.symbol} 价格1: {price_1}")
        except Exception as e:
            self.logger.error(f"❌ 初始化价格1失败: {e}", exc_info=True)

    # ================= 核心行情逻辑 =================

    def on_tick(self, tick: Optional[Dict] = None):
        """行情更新回调 - 负责：
        1. 根据价格1 + 当前行情，决定是否需要创建 A1/A2 触发单
        2. 同步持仓和挂单状态
        3. 止损由交易所原生 STOP-LIMIT 负责（入场成交后立即挂 SL），策略不再在 on_tick 中临时下止损单
        """
        try:
            # 1. 获取实时行情（加 try/except 防御）
            try:
                tick = self.exchange.get_ticker(self.pair)
            except Exception as e:
                self.logger.error(f"❌ 获取行情失败，跳过本次 tick: {e}", exc_info=True)
                return

            if not tick or "lastPrice" not in tick:
                self.logger.warning(f"❌ 无效的行情数据: {self.pair}, 数据: {tick}")
                return

            # ===== 动态反向模式（涨多做空 / 跌多做多 + 7.5% 等量加仓 + TP/SL）=====
            #if self.config.get("dynamic_reversal_mode") and self.config.get("trade_mode") in ("short_only", "long_only"):
                self._on_tick_dynamic_reversal(tick)
                return


            current_price = float(tick.get("lastPrice", 0.0))
            if current_price <= 0:
                self.logger.warning(f"❌ 无效的价格: {self.pair}, 价格: {current_price}")
                return

            # ===== 新增：止损后暂停/波动检测 =====
            if self._handle_pause_and_maybe_resume():
                return

            # ===== 新增：若止损限价单已下达但未成交 =====
            # 注意：根据你的要求，“暂停一小时”必须在【止损单成交之后】才发生；
            # 因此这里不再 return（不阻塞后续下单/循环），只做提示，并用于避免重复下止损单。
            if self._manual_sl_pending:
                self.logger.warning(
                    f"⏳ 手动止损限价单待成交中(tag={self._manual_sl_pending_tag}, order_id={self._manual_sl_pending_order_id})，不会提前进入1小时暂停"
                )

            # 2. 获取跟踪器 & 价格1
            tracker = self.symbol_trackers.get(self.pair)
            if not tracker:
                self.logger.error(f"❌ 跟踪器不存在: {self.pair}")
                return

            price_1 = tracker.price_1 if tracker.price_1 > 0 else current_price

            # ⭐ 新增：price_1 防傻保护 —— 和当前价差太大就重置为当前价
            if (
                price_1 <= 0
                or price_1 / current_price > 10
                or current_price / price_1 > 10
            ):
                self.logger.warning(
                    f"⚠️ price_1 与当前价相差过大，疑似脏数据，自动重置: "
                    f"old_price_1={price_1}, current_price={current_price}"
                )
                price_1 = current_price
                tracker.price_1 = current_price
                # 顺便把最新 price_1 写回状态
                self.update_state({f"{self.pair}_price_1": current_price})

            self.logger.info(
                f"📈 实时行情 - {self.pair}: 当前价格: {current_price:.4f}, 价格1: {price_1:.4f}"
            )

            # 3. 设置杠杆（⭐只在启动时设置一次；失败则每 60 秒重试一次，避免每个 tick 都打 REST）
            leverage = GridParams.LEVERAGE
            now_ts = time.time()
            if (not self._leverage_set) and (now_ts - self._leverage_last_try_ts >= 60):
                self._leverage_last_try_ts = now_ts
                if self.exchange.set_leverage(self.pair, leverage):
                    self._leverage_set = True
                    self.logger.info(f"✅ 成功设置 {self.pair} 杠杆为 {leverage} 倍")
                else:
                    self.logger.error(f"❌ 设置 {self.pair} 杠杆失败（将在 60 秒后重试一次）")


            # 4. 读取当前持仓，并统计 LONG / SHORT 数量
            positions = self.exchange.get_positions(self.pair)

            long_qty = 0.0
            short_qty = 0.0

            for p in positions or []:
                try:
                    if p.get("symbol") not in (self.pair, self.symbol):
                        continue

                    pos_side = (p.get("positionSide") or "").upper()
                    amt = float(p.get("positionAmt", "0") or 0)
                except Exception:
                    continue

                if pos_side == "LONG" and amt > 0:
                    long_qty += amt
                elif pos_side == "SHORT" and amt < 0:
                    short_qty += abs(amt)

            # ✅ “权威持仓状态”兜底：只要 ENTRY_FILLED 置位过，就认为已持仓（防 positions 延迟）
            long_position_open = (long_qty > 0) or self._authoritative_pos_open.get("long", False)
            short_position_open = (short_qty > 0) or self._authoritative_pos_open.get("short", False)

            # 5. 计算 A1/A2 的触发价 & 止盈/止损价（SL 只给策略内部用）
            a1_price = price_1 * (1 + GridParams.A1_ENTRY_OFFSET)   # A1：价格1上方
            a2_price = price_1 * (1 + GridParams.A2_ENTRY_OFFSET)   # A2：价格1下方

            a1_take_profit_price = price_1 * (1 + GridParams.A1_TP_OFFSET)
            a1_stop_loss_trigger = price_1 * (1 + GridParams.A1_SL_TRIGGER_OFFSET)

            a2_take_profit_price = price_1 * (1 + GridParams.A2_TP_OFFSET)
            a2_stop_loss_trigger = price_1 * (1 + GridParams.A2_SL_TRIGGER_OFFSET)

            # 6. 止损处理：
            # 如果启用了交易所原生 STOP-LIMIT 止损（USE_EXCHANGE_STOP_LOSS=False），
            # 则止损单会在【入场成交回调】中立即创建（AUTO_A1_SL / AUTO_A2_SL），
            # 不再在 on_tick 中用价格触发去“临时下止损单”，以避免重复/冲突。
            if not GridParams.USE_EXCHANGE_STOP_LOSS:
                # （兼容旧逻辑：价格触发时才下止损单）
                # LONG 止损：当前价 <= A1 止损触发价
                if long_position_open and current_price <= a1_stop_loss_trigger:
                    self.logger.warning(
                        f"⚠️ LONG 仓位触发策略止损: current={current_price}, "
                        f"trigger={a1_stop_loss_trigger}, qty={long_qty}"
                    )
                    try:
                        self.exchange.cancel_all_orders(symbol=self.pair)
                    except Exception as e:
                        self.logger.warning(f"撤销全部挂单失败(忽略继续执行止损): {e}")

                    stop_price = a1_stop_loss_trigger
                    limit_price = stop_price * GridParams.A1_SL_LIMIT_FACTOR

                    sl_order = self._create_and_check_order(
                        symbol=self.pair,
                        side="short",
                        order_type="limit",
                        quantity=long_qty,
                        price=limit_price,
                        params={
                            "positionSide": "LONG",
                            "tag": "MANUAL_A1_SL",
                            "triggerPrice": stop_price,
                        },
                    )

                    if sl_order:
                        tracker_sl_id = SymbolTracker.get_canonical_id_from_order(sl_order)
                        self._manual_sl_pending = True
                        self._manual_sl_pending_tag = "MANUAL_A1_SL"
                        self._manual_sl_pending_order_id = tracker_sl_id
                        self.logger.info(
                            f"✅ LONG 止损限价单已下达(等待成交): price={limit_price}, order_id={tracker_sl_id}, raw={sl_order}"
                        )
                        return
                    else:
                        self.logger.error("❌ LONG 仓位止损限价单创建失败（不会进入暂停）")

                # SHORT 止损：当前价 >= A2 止损触发价
                if short_position_open and current_price >= a2_stop_loss_trigger:
                    self.logger.warning(
                        f"⚠️ SHORT 仓位触发策略止损: current={current_price}, "
                        f"trigger={a2_stop_loss_trigger}, qty={short_qty}"
                    )
                    try:
                        self.exchange.cancel_all_orders(symbol=self.pair)
                    except Exception as e:
                        self.logger.warning(f"撤销全部挂单失败(忽略继续执行止损): {e}")

                    stop_price = a2_stop_loss_trigger
                    limit_price = stop_price * GridParams.A2_SL_LIMIT_FACTOR

                    sl_order = self._create_and_check_order(
                        symbol=self.pair,
                        side="long",
                        order_type="limit",
                        quantity=short_qty,
                        price=limit_price,
                        params={
                            "positionSide": "SHORT",
                            "tag": "MANUAL_A2_SL",
                            "triggerPrice": stop_price,
                        },
                    )

                    if sl_order:
                        tracker_sl_id = SymbolTracker.get_canonical_id_from_order(sl_order)
                        self._manual_sl_pending = True
                        self._manual_sl_pending_tag = "MANUAL_A2_SL"
                        self._manual_sl_pending_order_id = tracker_sl_id
                        self.logger.info(
                            f"✅ SHORT 止损限价单已下达(等待成交): price={limit_price}, order_id={tracker_sl_id}, raw={sl_order}"
                        )
                        return
                    else:
                        self.logger.error("❌ SHORT 仓位止损限价单创建失败（不会进入暂停）")

            # 7. 获取未成交订单，并同步 tracker 状态
            open_orders = self.order_manager.get_open_orders(self.pair)

            # ⚠️ 如果获取失败（None），不要清理 tracker，避免以为订单没了又重新下单
            if open_orders is None:
                self.logger.warning(
                    "⚠️ 获取未成交订单失败，本次不执行 cleanup_completed_orders，"
                    "以避免误删本地订单状态"
                )
            else:
                tracker.cleanup_completed_orders(open_orders, self.logger)

            # 有仓位但没有止盈保护时，自动补挂 TP（策略自身负责 SL，不再挂交易所 STOP_MARKET）
            self._ensure_protection_orders(open_orders, price_1)

            # 8. 计算下单数量（基于价格1）
            base_quantity = self._calculate_order_quantity(price_1)

            # 标记本轮是否有下单，用于统一更新冷却时间
            created_a1 = False
            created_a2 = False

            # 9/10. 单边入场：只挂 1 单（按 trade_mode 决定方向）
            if tracker.is_cooling_down(self.ORDER_COOLDOWN_SECONDS):
                self.logger.info("ℹ️ 仍在冷却期，跳过创建入场单")
            else:
                # 如果已持仓，就不再挂新的入场单
                if long_position_open or short_position_open:
                    self.logger.info("ℹ️ 已有持仓，跳过创建入场单")
                else:
                    # 如果已有任意方向的 A 级挂单，也不重复创建
                    has_active_long_a = tracker.has_active_order("long", "A")
                    has_active_short_a = tracker.has_active_order("short", "A")
                    if has_active_long_a or has_active_short_a:
                        self.logger.info("ℹ️ 已存在入场挂单(A级)，跳过重复创建")
                    else:
                        # 按 trade_mode 选择单边
                        if self.trade_mode == "short_only":
                            # 涨 -> 做空：挂在市价上方 0.1%
                            entry_side = "short"
                            position_side = "SHORT"
                            tag = "A2"
                            entry_price = price_1 * (1.0 + 0.001)
                        else:
                            # 跌 -> 做多：挂在市价下方 0.1%
                            entry_side = "long"
                            position_side = "LONG"
                            tag = "A1"
                            entry_price = price_1 * (1.0 - 0.001)

                        base_quantity = self._calculate_order_quantity(price_1)
                        entry_order = self._create_and_check_order(
                            symbol=self.pair,
                            side=entry_side,
                            order_type="limit",
                            quantity=base_quantity,
                            price=entry_price,
                            params={
                                "tag": tag,
                                "positionSide": position_side,
                                "timeInForce": "GTC",
                            },
                        )

                        if entry_order:
                            # 统一记录到 tracker 的 A 档
                            tracker.set_active_order(entry_side, "A", entry_order)
                            self.logger.info(
                                f"✅ 单边入场单创建成功: {tag} {entry_side} {base_quantity} @ {entry_price}"
                            )
                            # 统一更新冷却
                            tracker.update_last_order_time()
                        else:
                            self.logger.error("❌ 单边入场单创建失败")

            # 11. 如果本轮有任意一边下单成功，则更新冷却时间
            if created_a1 or created_a2:
                tracker.last_order_time = time.time()

        except Exception as e:
            self.logger.error(f"❌ 行情处理错误: {e}", exc_info=True)

    # 统一格式化策略日志：EVENT + 关键字段
    def _log_evt(self, log_level: str, event: str, **fields):
        """
        log_level: 'info' / 'warning' / 'error'
        event: 简短事件名，如 'CREATE_ORDER_OK' / 'AUTO_TP_MISS' 等
        fields: 关键字段，自动序列化成 key=value
        """
        parts = [f"[{self.symbol}]", f"{event}"]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        msg = " | ".join(parts)

        log_fn = getattr(self.logger, log_level, self.logger.info)
        log_fn(msg)

    # ================= 新增：止损后暂停 + 波动率检测 =================

    def _enter_sl_pause(self, reason: str) -> None:
        """止损后进入暂停状态：撤掉所有挂单，设置暂停到 now + 1h。"""
        try:
            self._pause_reason = reason or "STOP_LOSS"
            self._paused_until_ts = time.time() + GridParams.SL_PAUSE_SECONDS
            self._resume_needs_reset = True

            # 止损后立即撤掉挂单，避免在暂停期内误触发
            self._clear_all_open_orders()

            self._log_evt(
                "warning",
                "ENTER_SL_PAUSE",
                reason=self._pause_reason,
                paused_until_ts=self._paused_until_ts,
                paused_minutes=GridParams.SL_PAUSE_SECONDS / 60.0,
            )
        except Exception as e:
            self.logger.error(f"❌ 进入止损暂停状态失败: {e}", exc_info=True)

    def _is_paused(self) -> bool:
        return bool(self._paused_until_ts and time.time() < self._paused_until_ts)

    def _get_last_hour_amplitude_pct(self) -> Optional[float]:
        """获取过去 1 小时振幅（(high-low)/low）。使用交易所封装的 get_kline。"""
        try:
            if not hasattr(self.exchange, "get_kline"):
                self.logger.warning("exchange 未实现 get_kline，无法计算振幅")
                return None

            klines = self.exchange.get_kline(
                symbol=self.pair,
                interval=GridParams.VOL_CHECK_TIMEFRAME,
                limit=GridParams.VOL_CHECK_LIMIT,
            )
            if not klines:
                return None

            highs = []
            lows = []
            for k in klines:
                try:
                    highs.append(float(k.get("high")))
                    lows.append(float(k.get("low")))
                except Exception:
                    continue

            if not highs or not lows:
                return None
            high = max(highs)
            low = min(lows)
            if low <= 0:
                return None
            return (high - low) / low

        except Exception as e:
            self.logger.error(f"❌ 获取K线/计算振幅失败: {e}", exc_info=True)
            return None
    def _handle_pause_and_maybe_resume(self) -> bool:
        """在 on_tick 开头调用：
        - 若仍在暂停期，直接跳过交易逻辑（返回 True 表示已处理）
        - 若暂停期结束，检测上一小时振幅：
            - 振幅 > 阈值：继续暂停 1h
            - 振幅 <= 阈值：恢复并重置策略（重新开始循环）
        """
        try:
            if not self._paused_until_ts:
                return False

            now = time.time()
            if now < self._paused_until_ts:
                # 暂停中：不做任何交易动作，但可以打日志（不要太频繁）
                self._log_evt(
                    "info",
                    "PAUSED_SKIP_TICK",
                    reason=self._pause_reason,
                    seconds_left=round(self._paused_until_ts - now, 2),
                )
                return True

            # 到点了：做波动检测
            amp = self._get_last_hour_amplitude_pct()
            if amp is None:
                # 拿不到振幅就保守：继续暂停
                self._paused_until_ts = now + GridParams.SL_PAUSE_SECONDS
                self._log_evt(
                    "warning",
                    "PAUSE_EXTEND_NO_VOL_DATA",
                    reason=self._pause_reason,
                    next_check_in_seconds=GridParams.SL_PAUSE_SECONDS,
                )
                return True

            self._log_evt(
                "info",
                "PAUSE_VOL_CHECK",
                reason=self._pause_reason,
                amplitude_pct=round(amp * 100, 4),
                threshold_pct=GridParams.VOLATILITY_CHECK_THRESHOLD * 100,
            )

            if amp > GridParams.VOLATILITY_CHECK_THRESHOLD:
                self._paused_until_ts = now + GridParams.SL_PAUSE_SECONDS
                self._log_evt(
                    "warning",
                    "PAUSE_EXTEND_HIGH_VOL",
                    amplitude_pct=round(amp * 100, 4),
                    next_check_in_seconds=GridParams.SL_PAUSE_SECONDS,
                )
                return True

            # 波动低于阈值：恢复
            self._log_evt(
                "info",
                "PAUSE_END_RESUME",
                amplitude_pct=round(amp * 100, 4),
            )

            self._paused_until_ts = 0.0
            self._pause_reason = ""

            if self._resume_needs_reset:
                self._resume_needs_reset = False
                # 恢复时重置策略，重新取 price_1 并重挂单
                self._reset_strategy()

            return False

        except Exception as e:
            self.logger.error(f"❌ 暂停/恢复逻辑处理异常: {e}", exc_info=True)
            return False

    # ================= 下单 & 数量计算 =================

    def _create_and_check_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        params: Dict,
        max_retries: int = 3,
    ) -> Optional[Dict]:
        """
        创建订单（带简单重试），只要 order_manager.create_order 返回非 None 就认为成功。

        ➕ 兜底逻辑：
        - 如果交易所返回 reduceOnly 相关错误（常见 -1106: Parameter 'reduceonly' sent when not required），
          则自动去掉 params['reduceOnly'] 再重试一次。
        - 支持 price=None（比如 STOP_MARKET 止损单），此时会把 price 直接传给 exchange.create_order，
          由交易所层自己决定是否需要 price 参数。
        """
        # 注意：不要直接改动传入的 params，拷一份出来用
        effective_params: Dict = dict(params or {})

        for attempt in range(max_retries):
            try:
                # ✅ 条件单（止损/止盈等）在 Binance USD-M 需要走 Algo Order 端口，否则会报 -4120
                if str(order_type).lower() in {
                    "stop", "stop_limit", "stop_market",
                    "take_profit", "take_profit_limit", "take_profit_market",
                    "trailing_stop_market",
                }:
                    order = self._create_algo_conditional_order(
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        quantity=quantity,
                        price=price,
                        params=effective_params,
                    )
                else:
                    order = self.order_manager.create_order(
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        quantity=quantity,
                        price=price,
                        params=effective_params,
                    )

                if not order:
                    self.logger.warning(
                        f"❌ 订单创建失败 (尝试 {attempt + 1}/{max_retries})"
                    )
                    continue

                order_id = (
                    order.get("id")
                    or order.get("orderId")
                    or order.get("clientOrderId")
                )

                self.logger.info(
                    f"✅ 订单创建成功 (尝试 {attempt + 1}/{max_retries})，"
                    f"order_id={order_id}, params={effective_params}"
                )

                # 补充统一的 canonical_id 字段
                if order_id:
                    order["canonical_id"] = order_id

                return order

            except Exception as e:
                msg = str(e).lower()

                # 🔁 reduceOnly 兜底重试逻辑（加安全限制）
                # ⚠️ 对止损单（AUTO_/MANUAL_ + SL/STOP），绝不移除 reduceOnly 重试，
                #    避免极端情况下止损单变成“可能反向开仓单”
                if "reduceonly" in msg and effective_params.get("reduceOnly") is not None:
                    tag_u = str(effective_params.get("tag") or "").upper()
                    is_strategy_sl = (
                        ("AUTO_" in tag_u or "MANUAL_" in tag_u)
                        and ("SL" in tag_u or "STOP" in tag_u)
                    )

                    if is_strategy_sl:
                        self.logger.error(
                            f"🛑 安全拦截：止损单 reduceOnly 报错，不移除 reduceOnly 重试。"
                            f"order_type={order_type}, tag={effective_params.get('tag')}, err={e}"
                        )
                        return None

                    self.logger.warning(
                        f"⚠️ 检测到 reduceOnly 相关错误（{e}），"
                        f"将移除 reduceOnly 参数后重试一次 (attempt={attempt + 1}/{max_retries})"
                    )
                    effective_params = {
                        k: v for k, v in effective_params.items() if k != "reduceOnly"
                    }
                    continue

                # ⬇️ 非 reduceOnly 相关错误，按原逻辑处理
                self.logger.error(
                    f"❌ 订单创建过程中出错: {e} (尝试 {attempt + 1}/{max_retries})",
                    exc_info=True,
                )

        self.logger.error(f"💥 所有 {max_retries} 次订单创建尝试均失败")
        return None

    def _create_algo_conditional_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        params: Dict,
    ) -> Optional[Dict]:
        """使用 Binance USDⓈ-M 合约 Algo 条件单接口创建条件单（止损/止盈等）

        背景：2025-12-09 起，STOP/TAKE_PROFIT/TRAILING_STOP 等条件单不再支持走 /fapi/v1/order，
        需要改用 /fapi/v1/algoOrder，否则会报 APIError(code=-4120): Order type not supported for this endpoint.

        兼容性策略：
        - 优先调用 order_manager.create_algo_order（如果你的 OrderManager 已实现）
        - 其次调用 exchange.create_algo_order（如果你的 Exchange 已实现）
        - 再其次尝试调用 ccxt/binanceusdm 的私有方法 fapiPrivatePostAlgoOrder
        """
        p = dict(params or {})
        tag = p.get('tag') or p.get('clientAlgoId')
        stop_price = p.get("stopPrice") or p.get("stop_price") or p.get("triggerPrice")
        if stop_price is None:
            raise ValueError(f"Algo 条件单缺少 stopPrice/triggerPrice 参数: tag={p.get('tag')}, params={p}")

        # side: strategy 使用 long/short；Binance 使用 BUY/SELL
        side_norm = str(side).lower()
        if side_norm in ("long", "buy"):
            bin_side = "BUY"
        elif side_norm in ("short", "sell"):
            bin_side = "SELL"
        else:
            bin_side = str(side).upper()

        # orderType 映射：策略里常用 stop_limit / stop_market
        ot = str(order_type).lower()
        if ot in ("stop_limit", "stop"):
            bin_order_type = "STOP"
        elif ot in ("stop_market",):
            bin_order_type = "DISABLED_STOP_MARKET"
        elif ot in ("take_profit_limit", "take_profit"):
            bin_order_type = "TAKE_PROFIT"
        elif ot in ("take_profit_market",):
            bin_order_type = "TAKE_PROFIT_MARKET"
        elif ot in ("trailing_stop_market",):
            bin_order_type = "TRAILING_STOP_MARKET"
        else:
            # 保底：直接用原始字符串（上层已限定集合）
            bin_order_type = str(order_type).upper()

        # 组装 Algo 条件单参数（字段名以 Binance Algo Order API 为准）
        payload: Dict = {
            "symbol": symbol.replace("/", ""),
            "side": bin_side,
            "algoType": "CONDITIONAL",
            "type": bin_order_type,
            "quantity": quantity,
            "triggerPrice": stop_price,
        }
        # 可选：用 tag 作为 clientAlgoId（便于查询/撤单）。需满足 ^[\.A-Z\:/a-z0-9_-]{1,36}$
        if tag:
            payload["clientAlgoId"] = str(tag)[:36]

        # 让返回包含更多字段，便于上层记录 algoId
        payload.setdefault("newOrderRespType", "RESULT")

        # 限价型条件单需要 price + timeInForce
        if bin_order_type in ("STOP", "TAKE_PROFIT") and price is not None:
            payload["price"] = price
            payload["timeInForce"] = p.get("timeInForce") or "GTC"

        # 常用可选字段
        if "positionSide" in p and p["positionSide"]:
            payload["positionSide"] = p["positionSide"]
        # 止损/止盈通常是平仓单
        if "reduceOnly" in p:
            payload["reduceOnly"] = p["reduceOnly"]
        else:
            payload["reduceOnly"] = True

        # 标记：尽量把 tag 带到 clientAlgoId（如后续需要查询/撤单可用）
        tag = p.get("tag")
        if tag:
            # Binance 对 clientAlgoId 有长度/字符限制；这里做轻量清洗
            safe_tag = re.sub(r"[^A-Za-z0-9_\-]", "_", str(tag))[:36]
            payload["clientAlgoId"] = safe_tag

        # 1) 如果 OrderManager 已提供 algo 下单方法，优先用它（便于统一签名/域名/重试）
        if hasattr(self.order_manager, "create_algo_order") and callable(getattr(self.order_manager, "create_algo_order")):
            try:
                order = self.order_manager.create_algo_order(payload)  # type: ignore
                if isinstance(order, dict) and tag and "tag" not in order:
                    order["tag"] = tag
                return order
            except Exception as e:
                self.logger.warning(f"⚠️ order_manager.create_algo_order 失败，将尝试走 exchange/ccxt: {e}")

        # 2) 如果 Exchange 已提供 algo 下单方法
        if hasattr(self.exchange, "create_algo_order") and callable(getattr(self.exchange, "create_algo_order")):
            try:
                order = self.exchange.create_algo_order(**payload)  # type: ignore
                if isinstance(order, dict) and tag and "tag" not in order:
                    order["tag"] = tag
                return order
            except Exception as e:
                self.logger.warning(f"⚠️ exchange.create_algo_order 失败，将尝试走 ccxt 私有方法: {e}")

        # 3) 兼容 ccxt 的私有方法（binanceusdm）
        client = getattr(self.exchange, "client", None) or self.exchange
        for fn_name in (
            "fapiPrivatePostAlgoOrder",
            "fapiPrivatePostAlgoOrderV1",
            "fapiPrivatePostAlgoOrderV2",
            "fapiPrivate_post_algo_order",
        ):
            fn = getattr(client, fn_name, None)
            if callable(fn):
                try:
                    resp = fn(payload)
                    # 统一返回 dict，补上 tag 方便 tracker 识别
                    if isinstance(resp, dict) and tag and "tag" not in resp:
                        resp["tag"] = tag
                    return resp if isinstance(resp, dict) else {"info": resp, "tag": tag}
                except Exception as e:
                    self.logger.error(f"❌ 调用 {fn_name} 创建 Algo 条件单失败: {e}", exc_info=True)
                    break

        raise RuntimeError(
            "当前 exchange/order_manager 未实现 Algo 条件单接口。"
            "请在 OrderManager 或 Exchange 层实现 create_algo_order(payload) 或暴露 ccxt 的 fapiPrivatePostAlgoOrder。"
        )

    def _calculate_order_quantity(self, price: float) -> float:
        """计算下单数量（只使用价格1），并满足最小名义价值 / 步长等约束"""
        quote = 'USDT' if str(self.pair).upper().endswith('USDT') else 'USDC'
        bal = self.exchange.get_balance(quote)
        available_balance = bal.get(quote, {}).get('free', 0.0)


        leverage = GridParams.LEVERAGE
        used_funds = available_balance * GridParams.FUNDS_RATIO  # 使用  资金
        base_quantity = (used_funds * leverage) / price if price > 0 else 0.0

        min_amount = GridParams.MIN_AMOUNT
        step = GridParams.AMOUNT_STEP
        min_notional = GridParams.MIN_NOTIONAL
        min_quantity_for_notional = min_notional / price if price > 0 else 0.0

        base_quantity = max(base_quantity, min_amount, min_quantity_for_notional)

        # 按 0.001 步长向上取整
        multiplied = base_quantity * 1000
        if abs(multiplied - round(multiplied)) < 1e-9:
            base_quantity = round(base_quantity, 3)
        else:
            base_quantity = math.ceil(multiplied) / 1000

        final_notional = base_quantity * price
        self.logger.info(
            f"📊 数量计算 - 可用资金: {available_balance} USDC, 价格: {price}, 杠杆: {leverage}, "
            f"下单资金: {used_funds} USDC, 数量: {base_quantity} ETH, 名义价值: {final_notional} USDC"
        )

        return base_quantity

    def _get_protection_status(self, open_orders, position_side: str):
        """
        检查指定方向的持仓是否已经有止盈/止损保护单

        返回 (has_take_profit, has_stop_loss)

        现在主要通过 tag 识别：
        - 包含 AUTO_ 且 TP → 认为是止盈
        - 包含 AUTO_ 且 SL/STOP → 认为是止损
        type 判断只作为兜底兼容
        """
        has_tp = False
        has_sl = False

        if not open_orders:
            return has_tp, has_sl

        for order in open_orders:
            try:
                if not isinstance(order, dict):
                    continue

                # 只关心当前交易对
                symbol = order.get('symbol')
                if symbol not in (self.pair, self.symbol):
                    continue

                # 只关心对应方向的平仓单
                if order.get('positionSide') != position_side:
                    continue

                status = (order.get('status') or '').upper()
                # 没有状态字段就默认认为是活跃的
                if status and status not in ('NEW', 'PARTIALLY_FILLED'):
                    continue

                # 先看 tag（tag + clientOrderId 一起参与匹配）
                tag_from_api = str(order.get("tag") or "")
                client_order_id = str(order.get("clientOrderId") or "")
                if tag_from_api or client_order_id:
                    tag = f"{tag_from_api} {client_order_id}".upper()
                else:
                    tag = ""
                o_type = (order.get('type') or '').upper()

                # tag 识别
                if 'AUTO_' in tag and 'TP' in tag:
                    has_tp = True
                elif 'AUTO_' in tag and ('SL' in tag or 'STOP' in tag):
                    has_sl = True
                else:
                    # 兜底：兼容原来依赖 type 的逻辑
                    if o_type in ('TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'TAKE_PROFIT_LIMIT'):
                        has_tp = True
                    elif o_type in ('STOP', 'STOP_MARKET', 'STOP_LOSS', 'STOP_LOSS_LIMIT'):
                        has_sl = True

            except Exception:
                continue

        return has_tp, has_sl

    def _ensure_protection_orders(self, open_orders, price_1: float):
        """
        有仓位但没有止盈保护单时，自动补挂 TP LIMIT。

        ⚠️ 注意：
        - 不再在交易所挂 STOP_MARKET 止损单
        - 止损由策略在 on_tick 中根据价格触发，直接用 MARKET 单平仓
        """
        try:
            positions = self.exchange.get_positions(self.pair)
        except Exception as e:
            self.logger.error(f"❌ 获取持仓信息失败，无法自动补挂止盈: {e}", exc_info=True)
            return

        long_qty = 0.0
        short_qty = 0.0

        # 统计当前 LONG / SHORT 仓位数量
        for pos in positions or []:
            try:
                if pos.get("symbol") not in (self.pair, self.symbol):
                    continue

                pos_side = pos.get("positionSide", "BOTH")
                amt = float(pos.get("positionAmt", 0) or 0)
            except Exception:
                continue

            if pos_side in ("LONG", "BOTH") and amt > 0:
                long_qty += amt
            elif pos_side == "SHORT" and amt < 0:
                short_qty += abs(amt)

        # ========== LONG 方向保护（A1）—— 只补挂止盈 ==========
        if long_qty > 0:
            has_tp, has_sl = self._get_protection_status(open_orders, "LONG")
            # has_sl 这里仅用于日志说明，真实止损由 on_tick 处理
            if has_tp:
                self.logger.info("🛡 LONG 仓位已有止盈保护单，跳过自动补挂 TP")
            else:
                a1_take_profit_price = price_1 * (1 + GridParams.A1_TP_OFFSET)

                self.logger.info(
                    f"⚠️ 检测到 LONG 持仓 {long_qty} ETH 但没有止盈单，准备自动补挂 "
                    f"(TP_LIMIT={a1_take_profit_price})，止损由策略内市价止损负责"
                )

                # 补挂止盈：LIMIT 卖出平多
                try:
                    tp_order = self._create_and_check_order(
                        symbol=self.pair,
                        side="short",              # 卖出平多
                        order_type="limit",
                        quantity=long_qty,
                        price=a1_take_profit_price,
                        params={
                            "positionSide": "LONG",
                            "timeInForce": "GTC",
                            "tag": "AUTO_A1_TP",
                        },
                    )
                    if tp_order:
                        self.logger.info(f"✅ LONG 自动止盈 LIMIT 订单已创建: {tp_order}")
                    else:
                        self.logger.error("❌ LONG 自动止盈订单创建失败（_create_and_check_order 返回 None）")
                except Exception as e:
                    self.logger.error(f"❌ 创建 LONG 自动止盈订单失败: {e}", exc_info=True)

        # ========== SHORT 方向保护（A2）—— 只补挂止盈 ==========
        if short_qty > 0:
            has_tp, has_sl = self._get_protection_status(open_orders, "SHORT")
            if has_tp:
                self.logger.info("🛡 SHORT 仓位已有止盈保护单，跳过自动补挂 TP")
            else:
                a2_take_profit_price = price_1 * (1 + GridParams.A2_TP_OFFSET)

                self.logger.info(
                    f"⚠️ 检测到 SHORT 持仓 {short_qty} ETH 但没有止盈单，准备自动补挂 "
                    f"(TP_LIMIT={a2_take_profit_price})，止损由策略内市价止损负责"
                )

                # 补挂止盈：LIMIT 买入平空
                try:
                    tp_order = self._create_and_check_order(
                        symbol=self.pair,
                        side="long",               # 买入平空
                        order_type="limit",
                        quantity=short_qty,
                        price=a2_take_profit_price,
                        params={
                            "positionSide": "SHORT",
                            "timeInForce": "GTC",
                            "tag": "AUTO_A2_TP",
                        },
                    )
                    if tp_order:
                        self.logger.info(f"✅ SHORT 自动止盈 LIMIT 订单已创建: {tp_order}")
                    else:
                        self.logger.error("❌ SHORT 自动止盈订单创建失败（_create_and_check_order 返回 None）")
                except Exception as e:
                    self.logger.error(f"❌ 创建 SHORT 自动止盈订单失败: {e}", exc_info=True)

    def _on_entry_order_filled(self, level: str, side_key: str, order: Dict[str, Any]):
        """
        某一级入场单（A/B/C/D/E/F）完全成交后的处理逻辑：

        - 在同一方向挂下一层（例如 A -> B）
        - 统一“重挂止盈”：按当前【总仓位】 & 当前 level 的 tp_offset，挂一个全平仓 TP LIMIT 单
          （即：止盈触发时，把这个方向所有仓位一次性平掉）
        side_key: "long" / "short"
        level:    "A"..."F"
        """
        try:
            tracker = self.symbol_trackers.get(self.symbol)
            if not tracker:
                self._log_evt("error", "ENTRY_FILLED_NO_TRACKER", symbol=self.symbol)
                return

            price_1 = tracker.price_1
            if price_1 <= 0:
                # 如果 price_1 异常，就尝试用成交价兜底
                try:
                    price_1 = float(order.get("avgPrice") or order.get("price") or 0.0)
                except Exception:
                    price_1 = 0.0

            # ========= 1）按当前 level 的配置，重挂【统一止盈】 =========
            # 1.1 统计当前总仓位（long_qty / short_qty）
            try:
                positions = self.exchange.get_positions(self.pair)
            except Exception as e:
                self.logger.error(f"❌ ENTRY_FILLED 获取持仓失败，无法重挂止盈: {e}", exc_info=True)
                positions = []

            long_qty = 0.0
            short_qty = 0.0
            for p in positions or []:
                try:
                    if p.get("symbol") not in (self.pair, self.symbol):
                        continue
                    pos_side = (p.get("positionSide") or "").upper()
                    amt = float(p.get("positionAmt", "0") or 0)
                except Exception:
                    continue

                if pos_side in ("LONG", "BOTH") and amt > 0:
                    long_qty += amt
                elif pos_side == "SHORT" and amt < 0:
                    short_qty += abs(amt)

            # 当前方向的总仓位 + tp_offset
            pos_side = "LONG" if side_key == "long" else "SHORT"
            total_qty = long_qty if pos_side == "LONG" else short_qty
            
            # ✅ 兜底：如果仓位同步延迟导致 total_qty=0，用本次成交量估一个 total_qty
            if total_qty <= 0:
                try:
                    filled_qty = float(
                        order.get("executedQty")
                        or order.get("filled")
                        or order.get("amount")
                        or order.get("origQty")
                        or 0.0
                    )
                except Exception:
                    filled_qty = 0.0

                if filled_qty > 0:
                    total_qty = filled_qty
                    self._log_evt(
                        "warning",
                        "ENTRY_FILLED_POSITION_LAG_FALLBACK_QTY",
                        level=level,
                        side=side_key,
                        fallback_qty=total_qty,
                    )

            if total_qty > 0 and price_1 > 0:
                side_cfg_cur = self.ladder_config.get(side_key, {}).get(level, {}) or {}
                # 当前级别的 tp_offset：优先用该级的 tp_offset，没有就回退到整体 A1/A2 的默认
                if side_key == "long":
                    default_tp_offset = GridParams.A1_TP_OFFSET
                else:
                    default_tp_offset = GridParams.A2_TP_OFFSET

                tp_offset = float(side_cfg_cur.get("tp_offset", default_tp_offset))
                tp_price = price_1 * (1.0 + tp_offset)


                # ✅ 只取消旧 TP，不动 SL（避免刚挂的 SL 被撤）
                self._cancel_tp_orders_for_side(pos_side)

                # ✅ 入场成交后，立刻创建交易所原生 STOP-LIMIT 止损单（AUTO_A1_SL / AUTO_A2_SL）
                # - stopPrice：止损触发价（相对 price_1 的偏移）
                # - price：止损限价（相对 stopPrice 的价差因子）
                if False and GridParams.USE_EXCHANGE_STOP_LOSS and total_qty > 0 and price_1 > 0:
                    if side_key == "long":
                        sl_stop_price = price_1 * (1.0 + GridParams.A1_SL_TRIGGER_OFFSET)
                        sl_limit_price = sl_stop_price * GridParams.A1_SL_LIMIT_FACTOR
                        sl_side = "short"   # 卖出平多
                        sl_tag = "AUTO_A1_SL"
                        sl_pos_side = "LONG"
                    else:
                        sl_stop_price = price_1 * (1.0 + GridParams.A2_SL_TRIGGER_OFFSET)
                        sl_limit_price = sl_stop_price * GridParams.A2_SL_LIMIT_FACTOR
                        sl_side = "long"    # 买入平空
                        sl_tag = "AUTO_A2_SL"
                        sl_pos_side = "SHORT"

                    sl_order = self._create_and_check_order(
                        symbol=self.pair,
                        side=sl_side,
                        order_type="limit",
                        quantity=total_qty,
                        price=sl_limit_price,
                        params={
                            "positionSide": sl_pos_side,
                            "stopPrice": sl_stop_price,
                            "tag": sl_tag,
                        },
                    )

                    if sl_order:
                        self._log_evt(
                            "info",
                            "CREATE_AUTO_SL_OK",
                            level=level,
                            side=side_key,
                            qty=total_qty,
                            stop_price=sl_stop_price,
                            limit_price=sl_limit_price,
                            tag=sl_tag,
                        )
                    else:
                        self._log_evt(
                            "error",
                            "CREATE_AUTO_SL_FAILED",
                            level=level,
                            side=side_key,
                            qty=total_qty,
                            stop_price=sl_stop_price,
                            limit_price=sl_limit_price,
                            tag=sl_tag,
                        )

                # 再挂新的“全仓止盈单”
                if side_key == "long":
                    tp_side = "short"      # 卖出平多
                    tp_tag = f"AUTO_{level}_ALL_TP"
                else:
                    tp_side = "long"       # 买入平空
                    tp_tag = f"AUTO_{level}_ALL_TP"

                tp_order = self._create_and_check_order(
                    symbol=self.pair,
                    side=tp_side,
                    order_type="limit",
                    quantity=total_qty,
                    price=tp_price,
                    params={
                        "positionSide": pos_side,
                        "timeInForce": "GTC",
                        "tag": tp_tag,
                    },
                )

                if tp_order:
                    self._log_evt(
                        "info",
                        "REPLACE_TOTAL_TP_OK",
                        level=level,
                        side=side_key,
                        qty=total_qty,
                        tp_price=tp_price,
                        tag=tp_tag,
                    )
                else:
                    self._log_evt(
                        "error",
                        "REPLACE_TOTAL_TP_FAILED",
                        level=level,
                        side=side_key,
                        qty=total_qty,
                        tp_price=tp_price,
                    )
            else:
                self._log_evt(
                    "warning",
                    "ENTRY_FILLED_NO_POSITION_FOR_TP",
                    level=level,
                    side=side_key,
                    total_qty=total_qty,
                    price_1=price_1,
                )

            # ========= 2）挂下一单：只用“成交后 7.5% 间隔”的链式挂单 =========
            levels = GridParams.LEVELS
            try:
                idx = levels.index(level)
            except ValueError:
                self._log_evt("warning", "ENTRY_FILLED_INVALID_LEVEL", level=level)
                return

            if idx + 1 >= len(levels):
                self._log_evt("info", "ENTRY_FILLED_NO_NEXT_LEVEL", level=level, side=side_key)
                return

            next_level = levels[idx + 1]

            # ✅ 用本次入场成交价作为锚点（不是 price_1）
            try:
                filled_price = float(order.get("avgPrice") or order.get("price") or 0.0)
            except Exception:
                filled_price = 0.0

            if filled_price <= 0:
                # 兜底：用 price_1（极少数情况下 order 没带成交价）
                filled_price = price_1

            if filled_price <= 0:
                self._log_evt("error", "ENTRY_FILLED_INVALID_FILLED_PRICE", price_1=price_1)
                return

            step_pct = float(self._dyn_step_pct)  # 默认 0.075（7.5%）
            if step_pct <= 0:
                self._log_evt("error", "ENTRY_FILLED_INVALID_STEP_PCT", step_pct=step_pct)
                return

            # ✅ 下一单的挂单价：按“上一单成交价”的 ±7.5%
            if side_key == "long":
                entry_price = filled_price * (1.0 - step_pct)
                side = "long"
                position_side = "LONG"
                tag = f"{next_level}1"
            else:
                entry_price = filled_price * (1.0 + step_pct)
                side = "short"
                position_side = "SHORT"
                tag = f"{next_level}2"

            # ✅ 数量：等量加仓（和第一单同口径）
            base_quantity = self._calculate_order_quantity(filled_price)
            quantity = base_quantity

            if quantity <= 0:
                self._log_evt(
                    "warning",
                    "ENTRY_FILLED_INVALID_QUANTITY",
                    level=next_level,
                    side=side_key,
                    quantity=quantity,
                )
                return

            next_order = self._create_and_check_order(
                symbol=self.pair,
                side=side,
                order_type="limit",
                quantity=quantity,
                price=entry_price,
                params={
                    "tag": tag,
                    "positionSide": position_side,
                    "timeInForce": "GTC",
                },
            )

            if next_order:
                tracker.set_active_order(side_key, next_level, next_order)
                self._log_evt(
                    "info",
                    "NEXT_CHAIN_ORDER_CREATED",
                    cur_level=level,
                    next_level=next_level,
                    side=side_key,
                    qty=quantity,
                    filled_price=filled_price,
                    step_pct=step_pct,
                    price=entry_price,
                    tag=tag,
                )
            else:
                self._log_evt(
                    "error",
                    "NEXT_CHAIN_ORDER_CREATE_FAILED",
                    cur_level=level,
                    next_level=next_level,
                    side=side_key,
                )

        except Exception as e:
            self._log_evt("error", "ENTRY_FILLED_EXCEPTION", error=str(e))
            self.logger.error(f"❌ 处理阶梯入场单成交时异常: {e}", exc_info=True)

    # ================= 清空 / 重置 =================
    def _cancel_tp_orders_for_side(self, position_side: str) -> None:
        """
        只取消某个方向（LONG/SHORT）的【策略止盈单 TP】。
        规则：tag/clientOrderId 中包含 AUTO_ 且包含 TP
        不取消任何 SL
        """
        try:
            open_orders = self.order_manager.get_open_orders(self.pair)
            if not open_orders:
                return

            self.logger.info(f"📝 准备取消 {position_side} 方向的【策略止盈单 TP】")
            for order in open_orders:
                try:
                    if order.get("symbol") not in (self.pair, self.symbol):
                        continue
                    if order.get("positionSide") != position_side:
                        continue

                    tag_from_api = str(order.get("tag") or "")
                    client_order_id = str(order.get("clientOrderId") or "")
                    tag_raw = f"{tag_from_api} {client_order_id}".strip() if (tag_from_api or client_order_id) else ""
                    tag = tag_raw.upper()

                    oid = order.get("id") or order.get("orderId") or order.get("clientOrderId")
                    if not oid:
                        continue

                    # ✅ 只取消 TP：AUTO_ + TP
                    if ("AUTO_" not in tag) or ("TP" not in tag):
                        continue

                    if self.order_manager.cancel_order(oid, self.pair):
                        self.logger.info(f"✅ 已取消 {position_side} 方向【TP】: oid={oid}, tag={tag_raw}")
                    else:
                        self.logger.warning(f"⚠️ 取消 {position_side} 方向【TP】失败: oid={oid}, tag={tag_raw}")

                except Exception:
                    continue

        except Exception as e:
            self.logger.error(f"❌ 取消 {position_side} 方向 TP 时出错: {e}", exc_info=True)

    def _cancel_protection_orders_for_side(self, position_side: str) -> None:
        """
        取消当前交易对某个方向（LONG/SHORT）的所有【策略保护单】（止盈 + 止损）

        只动我们自己下的单：
        - tag 里包含 AUTO_
        - 且包含 _TP / _SL（或将来你约定的其它后缀）
        """
        try:
            open_orders = self.order_manager.get_open_orders(self.pair)
            if not open_orders:
                return

            self.logger.info(f"📝 准备取消 {position_side} 方向的【策略保护单】")
            for order in open_orders:
                try:
                    if order.get("symbol") not in (self.pair, self.symbol):
                        continue
                    if order.get("positionSide") != position_side:
                        continue

                    o_type = (order.get("type") or "").upper()

                    # tag + clientOrderId 组合识别，避免只看到 "AUTO"
                    tag_from_api = str(order.get("tag") or "")
                    client_order_id = str(order.get("clientOrderId") or "")
                    if tag_from_api or client_order_id:
                        tag_raw = f"{tag_from_api} {client_order_id}".strip()
                    else:
                        tag_raw = ""
                    tag = tag_raw.upper()

                    # ✅ 提取一个“主 tag”，用于识别 A1/B2 或 AUTO_xxx
                    # 优先用交易所回调的 tag_from_api，其次从 clientOrderId 拆
                    primary_tag = (tag_from_api or client_order_id or "").upper().strip()
                    # clientOrderId 通常是 "A1_时间戳" 或 "AUTO_F_ALL_TP_时间戳"
                    if "_" in primary_tag:
                        primary_tag = primary_tag.split("_", 1)[0] if primary_tag.startswith(("A","B","C","D","E","F","G","H")) else primary_tag

                    oid = (
                        order.get("id")
                        or order.get("orderId")
                        or order.get("clientOrderId")
                    )
                    if not oid:
                        continue

                    # ✅ 只把我们策略生成的 AUTO_*_TP / AUTO_*_SL 当保护单
                    is_protection = (
                        "AUTO_" in tag
                        and (
                            "_TP" in tag
                            or "_SL" in tag
                            or tag.endswith("_STOP")
                        )
                    )

                    if not is_protection:
                        self.logger.info(
                            f"⏭ 跳过非策略保护单: oid={oid}, type={o_type}, tag={tag_raw}"
                        )
                        continue

                    if self.order_manager.cancel_order(oid, self.pair):
                        self.logger.info(
                            f"✅ 已取消 {position_side} 方向【策略保护单】: "
                            f"oid={oid}, type={o_type}, tag={tag_raw}"
                        )
                    else:
                        self.logger.warning(
                            f"⚠️ 取消 {position_side} 方向【策略保护单】失败: "
                            f"oid={oid}, type={o_type}, tag={tag_raw}"
                        )
                except Exception:
                    # 单条出错不影响其他订单
                    continue

        except Exception as e:
            self.logger.error(
                f"❌ 取消 {position_side} 方向保护单时出错: {e}", exc_info=True
            )

    def _clear_all_open_orders(self) -> None:
        """清除当前交易对的所有未成交订单（使用 cancel_all_orders 一次性撤单）"""
        try:
            self.logger.info(
                f"📝 准备清除 {self.symbol} 所有未成交订单（使用 cancel_all_orders）"
            )

            ok = self.order_manager.cancel_all_orders(self.pair)

            if ok:
                self.logger.info("✅ 当前 symbol 所有挂单已清除")
            else:
                self.logger.warning(
                    "⚠️ cancel_all_orders 返回失败，可能仍有部分挂单没有被撤掉"
                )

        except Exception as e:
            self.logger.error(
                f"❌ 清除挂单失败: {e}",
                exc_info=True
            )

    def _reset_strategy(self) -> None:
        """重置策略：清空挂单，重新初始化价格1与 tracker"""
        try:
            self.logger.info("🔄 开始重置策略...")
            # 取消所有策略挂单
            self._clear_all_open_orders()

            # 重新获取价格1
            self._init_level_1_orders()

            # 重置 tracker 状态
            tracker = self.symbol_trackers.get(self.pair)
            if tracker:
                for side in ["long", "short"]:
                    for level in GridParams.LEVELS:
                        tracker.active_orders[side][level] = None
                tracker.last_order_time = 0.0
            
            # ✅ 清空“入场成交去重集合”，避免新一轮被上一轮污染
            with self._entry_filled_seen_lock:
                self._entry_filled_seen_ids.clear()
            # ✅ 清空“权威持仓状态”，避免新一轮误判为仍持仓
            self._authoritative_pos_open = {"long": False, "short": False}
    

            self.logger.info("✅ 策略重置完成")
        except Exception as e:
            self.logger.error(f"❌ 策略重置失败: {e}", exc_info=True)

    # ================== 止盈后：请求移除当前交易对（由框架执行 remove） ==================
    def _request_remove_current_pair(self, reason: str = "") -> None:
        """
        标记：本交易对策略需要被框架移除（只移除当前交易对）
        注意：这里不直接调用框架 remove（策略层拿不到 framework 引用）
        """
        try:
            self._request_remove_pair = True
            self._request_remove_pair_reason = str(reason or "")
            # 止盈后通常已经平仓，但为避免残留挂单，先清一次
            self._clear_all_open_orders()
            self.logger.info(f"🧹 已标记止盈后移除当前交易对策略: {self.pair} reason={reason}")
        except Exception as e:
            self.logger.error(f"❌ 标记移除当前交易对失败: {e}", exc_info=True)

    # ================= 订单更新回调 =================
    
    def on_order_update(self, order: Dict[str, Any]):
        """订单更新回调（现在以 tag 为主来识别止盈/止损）

        约定：
        - tag 包含 AUTO_ 且 TP   → 认为是策略止盈单
        - tag 包含 AUTO_ 且 SL/STOP → 认为是策略止损单

        兜底逻辑也要求 tag 带 AUTO_ 才算策略 TP/SL。
        止盈 / 止损成交后，一律调用 _reset_strategy() 重置整套策略。
        """
        try:
            # 原始订单整体打一条，方便排查
            self._log_evt("info", "ORDER_UPDATE_RAW", raw=order)

            symbol = order.get("symbol")
            if symbol != self.symbol:
                self._log_evt(
                    "info",
                    "ORDER_UPDATE_IGNORED_SYMBOL_MISMATCH",
                    order_symbol=symbol,
                    strategy_symbol=self.symbol,
                )
                return

            status = (order.get("status") or "").upper()
            order_type = (order.get("type") or "")
            order_type_upper = order_type.upper()
            side = (order.get("side") or "").upper()
            reduce_only = bool(order.get("reduceOnly"))

            # ⚠️ 这里改成：tag + clientOrderId 一起参与匹配
            # 有些时候交易所会把 tag 截断成 "AUTO"，真正的 AUTO_F_ALL_TP 在 clientOrderId 里
            tag_from_api = str(order.get("tag") or "")
            client_order_id = str(order.get("clientOrderId") or "")

            if tag_from_api or client_order_id:
                tag_raw = f"{tag_from_api} {client_order_id}".strip()
            else:
                tag_raw = ""
            tag = tag_raw.upper()

            # ✅ primary_tag：用于识别入场单 A1/B2...（不要用 tag_raw，因为它可能包含空格/两段拼接）
            primary_tag = (tag_from_api or client_order_id or "").upper().strip()
            # clientOrderId 通常是 "A1_时间戳" / "B2_时间戳" / "AUTO_F_ALL_TP_时间戳"
            # 入场单我们只需要前两位：A1/B2...
            if "_" in primary_tag:
                primary_tag = primary_tag.split("_", 1)[0]

            # 核心字段统一打一条
            self._log_evt(
                "info",
                "ORDER_UPDATE",
                symbol=symbol,
                type=order_type_upper,
                side=side,
                status=status,
                reduce_only=reduce_only,
                tag=tag,
            )

            # 如果是手动止损限价单，但未成交就被取消/拒绝/过期，清理 pending 让策略可以重试
            if ("MANUAL_A1_SL" in tag or "MANUAL_A2_SL" in tag) and status in ("CANCELED", "REJECTED", "EXPIRED"):
                if self._manual_sl_pending:
                    self._log_evt("warning", "MANUAL_SL_NOT_FILLED_CLEAR_PENDING", tag=tag_raw, status=status)
                    self._manual_sl_pending = False
                    self._manual_sl_pending_tag = None
                    self._manual_sl_pending_order_id = None
                return

# 只对 FILLED / CLOSED 做处理
            if status not in ("FILLED", "CLOSED"):
                return

            tracker = self.symbol_trackers.get(self.symbol)
            if not tracker:
                self._log_evt("error", "ORDER_UPDATE_NO_TRACKER", symbol=self.symbol)
                return

            # ===== 手动止损限价单成交 → 进入暂停/波动率检测机制 =====
            if "MANUAL_A1_SL" in tag or "MANUAL_A2_SL" in tag:
                reason = "MANUAL_A1_SL" if "MANUAL_A1_SL" in tag else "MANUAL_A2_SL"

                # 清理 pending 状态（只要命中手动止损成交就清理，避免策略卡住）
                if self._manual_sl_pending:
                    self._manual_sl_pending = False
                    self._manual_sl_pending_tag = None
                    self._manual_sl_pending_order_id = None

                self._log_evt("warning", "MANUAL_SL_FILLED_ENTER_PAUSE", tag=tag_raw, reason=reason)
                self._enter_sl_pause(reason=reason)
                return

            # ========= 1）优先用 tag 判断：止盈 =========
            if ("AUTO_" in tag) and ("TP" in tag):
                self._log_evt(
                    "info",
                    "AUTO_TP_FILLED_RESET_STRATEGY",
                    tag=tag_raw,
                    type=order_type_upper,
                    side=side,
                )
                # ✅ 最后保险：止盈成交意味着该方向仓位应结束，先清权威持仓标记，防止状态残留卡住不挂单
                pos_side_evt = (order.get("positionSide") or "").upper()
                if pos_side_evt == "LONG":
                    self._authoritative_pos_open["long"] = False
                elif pos_side_evt == "SHORT":
                    self._authoritative_pos_open["short"] = False

                self._request_remove_current_pair(reason="AUTO_TP_FILLED_REMOVE_PAIR")
                return

            # ========= 2）优先用 tag 判断：止损 =========
            # 约定：AUTO_A1_SL / AUTO_A2_SL 成交后进入“止损暂停 + 波动率检测”，而不是立刻重置。
            if "AUTO_A1_SL" in tag or "AUTO_A2_SL" in tag:
                reason = "AUTO_A1_SL" if "AUTO_A1_SL" in tag else "AUTO_A2_SL"
                self._log_evt(
                    "warning",
                    "AUTO_SL_FILLED_ENTER_PAUSE",
                    tag=tag_raw,
                    type=order_type_upper,
                    side=side,
                    reason=reason,
                )
                # ✅ 最后保险：止损成交意味着该方向仓位应结束，先清权威持仓标记
                pos_side_evt = (order.get("positionSide") or "").upper()
                if pos_side_evt == "LONG":
                    self._authoritative_pos_open["long"] = False
                elif pos_side_evt == "SHORT":
                    self._authoritative_pos_open["short"] = False

                self._enter_sl_pause(reason=reason)
                return

            # 其它 AUTO_*_SL / AUTO_*_STOP 的情况（如果未来扩展），默认按一轮结束处理：重置策略
            if "AUTO_" in tag and ("SL" in tag or "STOP" in tag):
                self._log_evt(
                    "warning",
                    "AUTO_SL_FILLED_RESET_STRATEGY",
                    tag=tag_raw,
                    type=order_type_upper,
                    side=side,
                )
                self._reset_strategy()
                return

            # ========= 3）兜底：基于 type 的判断（兼容非标准 tag 的策略 TP/SL） =========
            if (
                "AUTO_" in tag
                and order_type_upper
                in ["TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT_MARKET"]
            ):
                self._log_evt(
                    "info",
                    "TYPE_TP_FILLED_RESET_STRATEGY",
                    tag=tag_raw,
                    type=order_type_upper,
                    side=side,
                )
                self._reset_strategy()
                return

            if (
                "AUTO_" in tag
                and order_type_upper
                in ["STOP", "DISABLED_STOP_MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"]
                and reduce_only
            ):
                self._log_evt(
                    "warning",
                    "TYPE_STOP_SL_FILLED_RESET_STRATEGY",
                    tag=tag_raw,
                    type=order_type_upper,
                    side=side,
                )
                self._reset_strategy()
                return

            # ========= 3.3 兜底：reduceOnly 且已经完全平仓，也当作一轮结束 =========
            if reduce_only:
                try:
                    positions = self.exchange.get_positions(self.pair)
                except Exception as e:
                    self._log_evt(
                        "error",
                        "REDUCE_ONLY_CHECK_POSITION_FAILED",
                        error=str(e),
                    )
                else:
                    long_qty = 0.0
                    short_qty = 0.0

                    for p in positions or []:
                        try:
                            if p.get("symbol") not in (self.pair, self.symbol):
                                continue

                            pos_side = (p.get("positionSide") or "").upper()
                            amt = float(p.get("positionAmt", "0") or 0)
                        except Exception:
                            continue

                        if pos_side == "LONG" and amt > 0:
                            long_qty += amt
                        elif pos_side == "SHORT" and amt < 0:
                            short_qty += abs(amt)

                    if long_qty <= 0 and short_qty <= 0:
                        self._log_evt(
                            "info",
                            "REDUCE_ONLY_FLAT_RESET_STRATEGY",
                            tag=tag_raw,
                            side=side,
                        )
                        self._request_remove_current_pair(reason="TP_FLAT_REMOVE_PAIR")
                        return

            # ========= 4）其它情况：入场单成交 → 阶梯处理（挂下一层、重挂止盈） =========
            if primary_tag and (not primary_tag.startswith("AUTO_")):
                # 形如 A1 / B2 / C1 / ...
                if len(primary_tag) >= 2 and primary_tag[0] in GridParams.LEVELS and primary_tag[1] in ("1", "2"):

                    # ✅ 幂等去重：同一个入场单（orderId/clientOrderId）只触发一次
                    dedup_id = (
                        order.get("clientOrderId")
                        or order.get("orderId")
                        or order.get("id")
                        or order.get("canonical_id")
                    )
                    if dedup_id:
                        with self._entry_filled_seen_lock:
                            if dedup_id in self._entry_filled_seen_ids:
                                self._log_evt(
                                    "info",
                                    "ENTRY_LEVEL_FILLED_DUPLICATE_SKIP",
                                    tag=tag_raw,
                                    dedup_id=dedup_id,
                                    status=status,
                                )
                                return
                            self._entry_filled_seen_ids.add(dedup_id)

                    level = primary_tag[0]
                    side_key = "long" if primary_tag[1] == "1" else "short"

                    try:
                        if tracker:
                            tracker.clear_active_order(side_key, level)
                    except Exception:
                        pass

                    self._log_evt(
                        "info",
                        "ENTRY_LEVEL_FILLED",
                        level=level,
                        side=side_key,
                        tag=tag_raw,
                    )
                    # ✅ ENTRY_FILLED 作为权威持仓状态：先置位，避免下一次 on_tick 因 positions 延迟而重复开仓
                    self._authoritative_pos_open[side_key] = True

                    # ✅ 同时打一次冷却，避免 on_tick 在极短时间内重复挂 A1/A2
                    try:
                        tracker.last_order_time = time.time()
                    except Exception:
                        pass

                    self._on_entry_order_filled(level, side_key, order)
                    return

            return

        except Exception as e:
            self._log_evt("error", "ORDER_UPDATE_EXCEPTION", error=str(e))
            self.logger.error(f"❌ 订单更新处理错误: {e}", exc_info=True)

    def _on_ws_ticker(self, tick: Dict[str, Any]) -> None:
        """WebSocket 行情回调：用于让 exchange 缓存保持最新，策略侧可选做轻量记录"""
        try:
            # 这里只做缓存/观测，不在 WS 线程里做重逻辑（下单/大量 REST）
            self._last_ws_tick = tick
        except Exception:
            pass

    # ===================== 动态反向模式实现 =====================
    def _on_tick_dynamic_reversal(self, tick: Dict):
        """涨多做空 / 跌多做多：记录起始价，7.5% 等量加仓，更新 TP/SL。"""
        mark = float(tick.get("markPrice") or tick.get("lastPrice") or 0.0)
        if mark <= 0:
            return

        # 1) anchor（“起涨/起跌价”）：优先用 24h openPrice；没有就用 pct 反推
        if self._dyn_anchor_price is None:
            self._dyn_anchor_price = self._infer_anchor_price(tick, mark)

        # 2) 固定每单数量（第一次进场时计算一次）
        if self._dyn_fixed_qty is None:
            self._dyn_fixed_qty = self._calculate_order_quantity(mark)

        # 3) 当前仓位
        pos_side, pos_qty, liq_price = self._get_simple_position()

        mode = (self.config.get("trade_mode") or "both").lower()
        want_side = "short" if mode == "short_only" else "long"  # 本策略只会传 short_only/long_only

        # 4) 未持仓：立即按 mode 进第一单（市场价）
        if pos_qty <= 0:
            self._dyn_last_entry_price = mark
            self._dyn_next_add_price = self._calc_next_add_price(mark, want_side)

            try:
                self.order_manager.create_order(
                    symbol=self.pair,
                    side=want_side,
                    order_type="limit",
                    quantity=float(self._dyn_fixed_qty),
                    price=float(mark * (1.0 + 0.001) if want_side == "short" else mark * (1.0 - 0.001)),
                    params={"reduceOnly": False, "timeInForce": "GTC"},
                )
                self.logger.info(f"📌 动态反向进场 {self.pair} side={want_side} qty={self._dyn_fixed_qty} price≈{mark}")
            except Exception as e:
                self.logger.error(f"动态反向进场失败: {e}", exc_info=True)
                return

            # 进场后立刻挂 TP/SL（用当前 tick 估算；下一次 tick 会用真实 liqPrice 再校正）
            self._update_dyn_tp()
            # 不设止损：跳过更新 SL
            return

        # 5) 已持仓：若仍在同方向（避免外部手动操作导致方向反了）
        if pos_side != want_side:
            # 外部干预导致方向不一致：此模式下不自动处理
            return

        # 6) 7.5% 加仓：价格继续“朝亏损方向”走到阈值时加一单（等量）
        if self._dyn_next_add_price is None:
            self._dyn_next_add_price = self._calc_next_add_price(mark, want_side)

        should_add = (want_side == "short" and mark >= float(self._dyn_next_add_price)) or \
                     (want_side == "long" and mark <= float(self._dyn_next_add_price))

        if should_add:
            try:
                self.order_manager.create_order(
                    symbol=self.pair,
                    side=want_side,
                    order_type="limit",
                    quantity=float(self._dyn_fixed_qty),
                    price=float(mark * (1.0 + 0.001) if want_side == "short" else mark * (1.0 - 0.001)),
                    params={"reduceOnly": False, "timeInForce": "GTC"},
                )
                self._dyn_last_entry_price = mark
                self._dyn_next_add_price = self._calc_next_add_price(mark, want_side)
                self.logger.info(f"➕ 加仓 {self.pair} side={want_side} qty={self._dyn_fixed_qty} price≈{mark} next≈{self._dyn_next_add_price}")
            except Exception as e:
                self.logger.error(f"动态反向加仓失败: {e}", exc_info=True)
                return

            # 每次加仓后更新 TP/SL
            self._update_dyn_tp()
            # 不设止损：跳过更新 SL
        else:
            # 也定期校正 SL（liqPrice 可能变化）
            # 不设止损：跳过更新 SL
    def _infer_anchor_price(self, tick: Dict, mark: float) -> float:
        try:
            op = float(tick.get("openPrice") or 0.0)
            if op > 0:
                return op
        except Exception:
            pass
        try:
            pct = float(tick.get("priceChangePercent") or 0.0) / 100.0
            if abs(pct) > 1e-9:
                return mark / (1.0 + pct)
        except Exception:
            pass
        return mark

    def _calc_next_add_price(self, last_entry_price: float, side: str) -> float:
        step = float(self._dyn_step_pct or 0.075)
        if side == "short":
            return last_entry_price * (1.0 + step)
        return last_entry_price * (1.0 - step)

    def _get_simple_position(self):
        """返回 (side, qty, liquidationPrice)。qty=0 表示无仓位。"""
        try:
            positions = self.exchange.get_positions(self.pair) or []
        except Exception:
            return (None, 0.0, None)

        best = None
        for p in positions:
            try:
                amt = float(p.get("positionAmt") or 0.0)
            except Exception:
                continue
            if abs(amt) > 0:
                best = p
                break

        if not best:
            return (None, 0.0, None)

        amt = float(best.get("positionAmt") or 0.0)
        side = "long" if amt > 0 else "short"
        qty = abs(amt)
        liq = None
        try:
            lp = float(best.get("liquidationPrice") or 0.0)
            if lp > 0:
                liq = lp
        except Exception:
            liq = None
        return (side, qty, liq)

    def _update_dyn_tp(self):
        """止盈：anchor 与最近一次入场价的中点。"""
        if self._dyn_anchor_price is None or self._dyn_last_entry_price is None:
            return
        tp_price = (float(self._dyn_anchor_price) + float(self._dyn_last_entry_price)) / 2.0
        if tp_price <= 0:
            return

        pos_side, pos_qty, _ = self._get_simple_position()
        if pos_qty <= 0:
            return

        exit_side = "long" if pos_side == "short" else "short"  # 买入平空 / 卖出平多

        # 取消旧 TP
        if self._dyn_tp_order_id:
            try:
                self.order_manager.cancel_order(self._dyn_tp_order_id)
            except Exception:
                pass
            self._dyn_tp_order_id = None

        try:
            o = self.order_manager.create_order(
                symbol=self.pair,
                side=exit_side,
                order_type="limit",
                quantity=float(pos_qty),
                price=float(tp_price),
                params={"reduceOnly": True},
            )
            self._dyn_tp_order_id = (o or {}).get("orderId") or (o or {}).get("id")
        except Exception as e:
            self.logger.error(f"更新 TP 失败: {e}", exc_info=True)

    def _update_dyn_sl(self, liquidation_price):
        """不设止损：保留接口但不下任何止损单。"""
        return

