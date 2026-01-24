# -*- coding: utf-8 -*-
"""量化交易框架主程序"""
import logging
import time
import signal
import sys
import importlib
import copy
from typing import Dict, Any, List

from core.strategy_engine import StrategyEngine
from core.config_manager import ConfigManager
from core.logger import Logger, Monitor
from core.order_manager import OrderManager
from core.risk_manager import RiskManager
from core.event_engine import EventEngine
from core.data_processor import DataProcessor
from exchanges.binance_exchange import BinanceExchange


class TradingFramework:
    """量化交易框架主类"""

    def __init__(self, config_path: str = "configs/config.yaml"):
        # 初始化日志，设置为INFO级别以减少日志量
        self.logger = Logger(log_dir="logs", log_level=logging.INFO).get_logger(__name__)

        # 初始化监控
        self.monitor = Monitor()

        # 加载配置
        self.config_manager = ConfigManager(config_path)
        self.config = self.config_manager.config

        # ===== 动态交易对选择（Top 幅度，涨多做空/跌多做多）=====
        dp = (self.config.get("dynamic_pairs") or {})
        self.dynamic_pairs_enabled = bool(dp.get("enabled", True))
        self.pair_refresh_interval = int(dp.get("refresh_interval", 600))
        self.max_dynamic_pairs = int(dp.get("max_pairs", 3))
        self.min_abs_pct = float(dp.get("min_abs_pct", 50.0))
        self.pos_threshold_short = float(dp.get("pos_threshold_short", 0.75))
        self.pos_threshold_long = float(dp.get("pos_threshold_long", 0.25))
        self.max_retrace_ratio = float(dp.get("max_retrace_ratio", 0.30))
        self.preselect_limit = int(dp.get("preselect_limit", 30))

        # 以配置中的第一个策略作为模板（动态生成时会复制并替换 symbol/pair/trade_mode）
        self._strategy_template_cfg = None
        try:
            strategies_cfg = self.config.get("strategies") or []
            if strategies_cfg:
                self._strategy_template_cfg = copy.deepcopy(strategies_cfg[0])
        except Exception:
            self._strategy_template_cfg = None

        # 开启动态交易对时，避免引擎在初始化阶段加载固定策略
        if self.dynamic_pairs_enabled:
            self.config["strategies"] = []
            self._dynamic_strategies: Dict[str, Any] = {}  # pair -> strategy instance

        # 初始化事件引擎
        self.event_engine = EventEngine()

        # 初始化交易所
        self.exchange = BinanceExchange(self.config["exchanges"]["binance"], self.config.get("global", {}))

        # 初始化风险管理器
        self.risk_manager = RiskManager(self.config["risk_manager"])
        self.risk_manager.initialize(self.exchange)

        # 初始化订单管理器
        self.order_manager = OrderManager(self.config)
        self.order_manager.initialize(self.exchange)
        self.order_manager.risk_manager = self.risk_manager

        # 初始化数据处理器
        self.data_processor = DataProcessor()

        # 初始化策略引擎
        self.strategy_engine = StrategyEngine(self.config)
        self.strategy_engine.initialize(self.exchange, self.order_manager, self.risk_manager, self.data_processor)

        # 注册信号处理
        self._register_signal_handlers()

        # 框架状态
        self.running = False

        self.logger.info("量化交易框架初始化完成")

    def _register_signal_handlers(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info(f"收到信号: {signum}, 正在停止框架...")
        self.stop()

    def start(self):
        try:
            self.logger.info("启动量化交易框架")
            self.running = True

            # 启动策略引擎
            self.strategy_engine.start()

            # 启动后先同步一次动态交易对
            if getattr(self, "dynamic_pairs_enabled", False):
                self._sync_dynamic_pairs()

            # 启动框架主循环
            self._main_loop()

        except Exception as e:
            self.logger.error(f"框架启动失败: {e}")
            self.stop()

    def _can_remove_pair(self, pair: str) -> bool:
        """移除条件：只看是否无持仓（你最新要求）"""
        try:
            positions = self.exchange.get_positions(pair) or []
            if isinstance(positions, dict):
                positions = [positions]
            for p in positions:
                try:
                    amt = float(p.get("positionAmt") or p.get("amt") or 0.0)
                except Exception:
                    amt = 0.0
                if abs(amt) > 0:
                    return False
        except Exception:
            # 取不到持仓就保守：不移除
            return False
        return True

    def _sync_dynamic_pairs(self):
        """
        动态维护交易对集合（USDT）：
        1) 运行交易对 < 3：从“>=min_abs_pct 且通过(72h+10d)形态过滤”的候选里补齐到 3
        2) 若出现新交易对 abs(24h%) > 当前运行中最大幅度：允许额外加入（最多 5）
        3) 若当前无运行且没有任何 >=min_abs_pct：仅选择当前幅度最大的 1 个（兜底）
        4) 移除老交易对：只要该交易对不在 target_set 且已经无持仓，就移除（不要求无挂单）
           （建议 stop=True 以撤销旧挂单，避免之后意外成交重新开仓）
        """
        if not getattr(self, "dynamic_pairs_enabled", False):
            return
        if not self._strategy_template_cfg:
            self.logger.warning("dynamic_pairs 已启用，但找不到策略模板（config.strategies[0]）。")
            return

        max_base = int((self.config.get("dynamic_pairs") or {}).get("max_pairs_base", 2))
        max_burst = int((self.config.get("dynamic_pairs") or {}).get("max_pairs_burst", 3))

        # 1) 拉候选：>=min_abs_pct（已内置 72h+10d + 回撤比例过滤）
        pins = self.exchange.get_pinbar_pairs_usdt(
            interval="1h",
            lookback_bars=6,
            cache_ttl=60,
        ) or []

        # pins: List[Dict] each contains symbol/mode/bar_index/score...
        geN = pins


        current_pairs = set(self._dynamic_strategies.keys())
        # 当前运行中最大 pinbar score（pinbar 版本不依赖 ticker 幅度）
        current_max_score = 0.0

        desired = set()

        if geN:
            if current_pairs:
                desired |= current_pairs

                # 不足 3 -> 从候选补齐
                for c in geN:
                    if len(desired) >= max_base:
                        break
                    sym = c.get("symbol")
                    if sym and sym not in desired:
                        desired.add(sym)

                # 幅度超过当前最大 -> 允许额外加入到 5
                for c in geN:
                    if len(desired) >= max_burst:
                        break
                    sym = c.get("symbol")
                    if not sym or sym in desired:
                        continue
                    desired.add(sym)
            else:
                # 当前无运行：直接取 top3（不足则取全部）
                for c in geN[:max_base]:
                    sym = c.get("symbol")
                    if sym:
                        desired.add(sym)
        else:
            # 没有任何 >=min_abs_pct 的候选
            # ✅ 按新规则：不兜底；如果没有 >=min_abs_pct 就不选任何交易对
            if current_pairs:
                desired |= current_pairs
            # else: desired 保持为空（不新增策略）

        # 最大不超过 3
        if len(desired) > max_burst:
            score_map = {c.get("symbol"): float(c.get("score") or 0.0) for c in geN if c.get("symbol")}
            keep = sorted(list(desired), key=lambda s: score_map.get(s, 0.0), reverse=True)[:max_burst]
            desired = set(keep)

        target_set = set([p for p in desired if p])

        # === 计算每个 target 的 trade_mode（short_only / long_only）===
        mode_map = {c.get("symbol"): c.get("mode") for c in geN if c.get("symbol")}
        for p in target_set:
            if p in mode_map and mode_map[p]:
                continue
            try:
                t = self.exchange.get_ticker(p) or {}
                pct = float(t.get("priceChangePercent") or 0.0)
                mode_map[p] = "short" if pct >= 0 else "long"
            except Exception:
                mode_map[p] = "short"

        def _trade_mode(m: str) -> str:
            return "short_only" if (m or "").startswith("short") else "long_only"

        # 1) 新增：目标里有、当前没有
        for pair in list(target_set - current_pairs):
            cfg = copy.deepcopy(self._strategy_template_cfg)
            cfg["symbol"] = pair
            cfg["pair"] = pair
            cfg["trade_mode"] = _trade_mode(mode_map.get(pair) or "short")
            cfg["dynamic_reversal_mode"] = True

            full_class_path = cfg.get("class")
            if not full_class_path:
                self.logger.error("策略模板缺少 class 字段，无法动态创建策略")
                continue
            module_path, class_name = full_class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            strategy_class = getattr(module, class_name)

            s = strategy_class(cfg, self.exchange, self.order_manager, self.risk_manager, self.data_processor)
            self.strategy_engine.add_strategy(s)
            self._dynamic_strategies[pair] = s
            self.logger.info(f"✅ 动态新增策略: {pair} mode={cfg.get('trade_mode')}")

        # 2) 移除：当前有、但不在目标集合里（只看无持仓）
        for pair in list(current_pairs - target_set):
            s = self._dynamic_strategies.get(pair)
            if not s:
                self._dynamic_strategies.pop(pair, None)
                continue

            if self._can_remove_pair(pair):
                # stop=True：更安全（会让策略退出并撤销它管理的挂单）
                self.strategy_engine.remove_strategy(s, stop=True)
                self._dynamic_strategies.pop(pair, None)
                self.logger.info(f"🧹 动态移除策略(无持仓): {pair}")
            else:
                self.logger.info(f"⏸️ {pair} 仍有持仓，暂不移除")

    def _main_loop(self):
        self.logger.info("进入主循环")

        STATUS_INTERVAL = 60
        METRICS_INTERVAL = 300
        PAIR_REFRESH_INTERVAL = getattr(self, "pair_refresh_interval", 600)

        last_status_time = time.time()
        last_metrics_time = time.time()
        last_pair_refresh_time = 0

        while self.running:
            try:
                current_time = time.time()

                if current_time - last_status_time >= STATUS_INTERVAL:
                    self._print_status()
                    last_status_time = current_time

                if current_time - last_metrics_time >= METRICS_INTERVAL:
                    self._print_metrics()
                    last_metrics_time = current_time

                if self.dynamic_pairs_enabled and (current_time - last_pair_refresh_time >= PAIR_REFRESH_INTERVAL):
                    try:
                        self._sync_dynamic_pairs()
                    except Exception as e:
                        self.logger.error(f"动态交易对刷新失败: {e}")
                    last_pair_refresh_time = current_time

                if not self.strategy_engine.is_running():
                    self.logger.warning("策略引擎已停止，正在重启...")
                    self.strategy_engine.start()
                # === 止盈后：策略请求移除当前交易对（只移除当前 pair）===
                if getattr(self, "dynamic_pairs_enabled", False):
                    for pair, s in list(getattr(self, "_dynamic_strategies", {}).items()):
                        if getattr(s, "_request_remove_pair", False):
                            # 保险：如果交易所持仓还没同步到 0，就先不移除，下一轮再试
                            if not self._can_remove_pair(pair):
                                self.logger.info(f"⏳ {pair} 已请求移除但持仓未确认归零，稍后重试")
                                continue

                            self.strategy_engine.remove_strategy(s, stop=True)
                            self._dynamic_strategies.pop(pair, None)
                            self.logger.info(
                                f"🧹 止盈后动态移除策略: {pair} reason={getattr(s, '_request_remove_pair_reason', '')}"
                            )

                time.sleep(1)

            except Exception as e:
                self.logger.error(f"主循环异常: {e}")
                time.sleep(5)

    def stop(self):
        if not self.running:
            return
        self.logger.info("停止量化交易框架")
        self.running = False
        self.strategy_engine.stop()
        self._print_metrics()
        self.logger.info("量化交易框架已停止")

    def _print_status(self):
        self.logger.info("=" * 50)
        self.logger.info("量化交易框架状态")
        self.logger.info(f"框架运行状态: {'运行中' if self.running else '已停止'}")
        self.logger.info(f"策略引擎状态: {'运行中' if self.strategy_engine.is_running() else '已停止'}")
        self.logger.info(f"活跃策略数: {len(self.strategy_engine.strategies)}")

        for i, strategy in enumerate(self.strategy_engine.strategies):
            status = strategy.get_status()
            run_status = "✅" if status.get("running") else "❌"
            self.logger.info(
                f"🔄 策略 {i+1}: {status.get('name')} {run_status} - {status.get('symbol')} PNL: {float(status.get('pnl') or 0.0):.2f}"
            )
            self.logger.debug(f"   详细状态: {status}")

        self.logger.info("=" * 50)

    def _print_metrics(self):
        self.logger.info("\n" + "=" * 50)
        self.logger.info("监控指标")
        self.logger.info("=" * 50)

        metrics = self.monitor.get_metrics()
        self.logger.info(f"运行时间: {metrics.get('uptime', 0.0):.2f}秒")

        self.logger.info("\n订单统计:")
        for key, value in (metrics.get("orders") or {}).items():
            self.logger.info(f"  {key}: {value}")

        self.logger.info("\n持仓统计:")
        for key, value in (metrics.get("positions") or {}).items():
            self.logger.info(f"  {key}: {value}")

        self.logger.info("\n盈亏统计:")
        for key, value in (metrics.get("pnl") or {}).items():
            self.logger.info(f"  {key}: {float(value or 0.0):.2f}")

        self.logger.info("\n错误统计:")
        for key, value in (metrics.get("errors") or {}).items():
            self.logger.info(f"  {key}: {value}")

        self.logger.info("\n" + "=" * 50)

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "strategy_engine_running": self.strategy_engine.is_running(),
            "active_strategies": len(self.strategy_engine.strategies),
            "metrics": self.monitor.get_metrics(),
        }


if __name__ == "__main__":
    try:
        print("开始初始化交易框架...")
        framework = TradingFramework(config_path="configs/config.yaml")
        print("交易框架初始化完成，开始启动...")
        framework.start()
    except KeyboardInterrupt:
        print("\n用户中断，退出程序")
    except Exception as e:
        print(f"程序异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("程序已退出")
