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


class ScannerMixin:
    def get_top_reversal_pairs_usdt(
        self,
        top_n: int = 3,
        min_abs_pct: float = 50.0,
        fallback_top1_if_none: bool = True,
        # 位置过滤：做空需贴近高位；做多需贴近低位
        pos_threshold_short: float = 0.75,
        pos_threshold_long: float = 0.25,
        # 回撤/反弹过滤（用于避开“先大涨后大跌 / 先大跌后大涨”已走完的行情）
        max_retrace_ratio: float = 0.30,
        # 多周期：72h 用 1h K线；10天用 1d K线(10根)
        kline_72h_interval: str = "1h",
        kline_72h_bars: int = 72,
        kline_10d_interval: str = "1d",
        kline_10d_bars: int = 10,
        # 性能/限流：只对候选集拉 K 线
        preselect_limit: int = 30,
        cache_ttl: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        选出“涨多做空 / 跌多做多”的候选交易对（USDT 永续）：
        1) 先用 futures_ticker 的 24h 涨跌幅筛：abs(pct) >= min_abs_pct
        2) 再用 72h(1h K线) + 10天(1d K线) 做“位置(pos) + 回撤/反弹比例”过滤：
           - 做空(pct>0)：需同时满足 pos72、pos24(取72h最后24根) 与 pos10d 都贴近高位，
             且从各周期高位回撤比例 (high-last)/high 不超过 max_retrace_ratio
           - 做多(pct<0)：需同时满足 pos72、pos24 与 pos10d 都贴近低位，
             且从各周期低位反弹比例 (last-low)/low 不超过 max_retrace_ratio
        返回：按 abs_pct 降序的 dict 列表，每项包含 symbol/pct/mode 等字段。
        """
        now = time.time()

        # cache（避免频繁打 REST）
        cache_key = (
            f"toprev_usdt_{top_n}_{min_abs_pct}_{pos_threshold_short}_{pos_threshold_long}_"
            f"{max_retrace_ratio}_{kline_72h_interval}_{kline_72h_bars}_{kline_10d_interval}_{kline_10d_bars}_{preselect_limit}"
        )
        if not hasattr(self, "_top_reversal_cache"):
            self._top_reversal_cache = {}
        cached = self._top_reversal_cache.get(cache_key)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) <= max(1, int(cache_ttl))):
            return cached.get("data") or []

        # 冷却期：不要打 REST（返回缓存或空）
        try:
            if self._is_in_rate_limit_cooldown():
                return cached.get("data") if cached else []
        except Exception:
            pass

        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        def _pos(last: float, lo: float, hi: float) -> float:
            if hi <= lo:
                return 0.5
            return (last - lo) / (hi - lo)

        def _window_stats(klines: List[Dict[str, Any]]):
            if not klines:
                return None
            lo = min(float(k.get("low", 0.0) or 0.0) for k in klines)
            hi = max(float(k.get("high", 0.0) or 0.0) for k in klines)
            last = float(klines[-1].get("close", 0.0) or 0.0)
            return lo, hi, last

        # 1) 取全市场 24h ticker（USDT 永续）
        try:
            tickers = self.client.futures_ticker() if not self.dry_run else []
        except Exception as e:
            try:
                self._handle_rate_limit_error(e, context="get_top_reversal_pairs_usdt:futures_ticker")
            except Exception:
                pass
            return []

        prelim: List[Dict[str, Any]] = []
        all_usdt: List[Dict[str, Any]] = []

        for t in (tickers or []):
            sym = str(t.get("symbol") or "")
            if not sym.endswith("USDT"):
                continue

            pct = _sf(t.get("priceChangePercent"))
            abs_pct = abs(pct)

            # 记录所有（用于兜底 top1）
            all_usdt.append({"symbol": sym, "pct": pct, "abs_pct": abs_pct})

            if abs_pct < float(min_abs_pct):
                continue

            mode = "short" if pct >= 0 else "long"
            prelim.append({"symbol": sym, "pct": pct, "abs_pct": abs_pct, "mode": mode})

        # 2) 没有 >= min_abs_pct 的候选：兜底 top1（不做形态过滤）
        if not prelim and fallback_top1_if_none:
            all_usdt.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
            if all_usdt:
                prelim = [dict(all_usdt[0])]
                prelim[0]["mode"] = "short" if float(prelim[0].get("pct") or 0.0) >= 0 else "long"
                prelim[0]["fallback"] = True

        prelim.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
        prelim = prelim[: max(1, int(preselect_limit))]

        results: List[Dict[str, Any]] = []

        for c in prelim:
            sym = c["symbol"]
            mode = c["mode"]

            # --- 72h: 1h K线（limit=72） ---
            kl72 = self.get_kline(sym, interval=kline_72h_interval, limit=int(kline_72h_bars)) or []
            if not kl72:
                if c.get("fallback"):
                    results.append(c)
                continue

            stats72 = _window_stats(kl72)
            if not stats72:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo72, hi72, last = stats72

            # 24h 区间：直接取 72h K线的最后 24 根（避免只看 ticker 的 24h 高低价导致跨24h尖峰丢失）
            kl24 = kl72[-24:] if len(kl72) >= 24 else kl72
            stats24 = _window_stats(kl24) if kl24 else None
            if not stats24:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo24, hi24, last24 = stats24

            # --- 10天: 1d K线10根 ---
            kl10d = self.get_kline(sym, interval=kline_10d_interval, limit=int(kline_10d_bars)) or []
            if not kl10d:
                if c.get("fallback"):
                    results.append(c)
                continue
            stats10 = _window_stats(kl10d)
            if not stats10:
                if c.get("fallback"):
                    results.append(c)
                continue
            lo10, hi10, last10 = stats10

            # 用最新 close（1h）为 last；日线 last 仅用于统计窗口
            pos72 = _pos(last, lo72, hi72)
            pos24 = _pos(last, lo24, hi24)
            pos10 = _pos(last, lo10, hi10)

            # 回撤/反弹比例
            pullback72 = (hi72 - last) / hi72 if hi72 > 0 else 0.0
            pullback24 = (hi24 - last) / hi24 if hi24 > 0 else 0.0
            pullback10 = (hi10 - last) / hi10 if hi10 > 0 else 0.0

            rebound72 = (last - lo72) / lo72 if lo72 > 0 else 0.0
            rebound24 = (last - lo24) / lo24 if lo24 > 0 else 0.0
            rebound10 = (last - lo10) / lo10 if lo10 > 0 else 0.0

            ok = True
            if mode == "short":
                # 仍贴近高位 + 回撤不能太大
                if (pos72 < float(pos_threshold_short)) or (pos24 < float(pos_threshold_short)) or (pos10 < float(pos_threshold_short)):
                    ok = False
                if (pullback72 > float(max_retrace_ratio)) or (pullback24 > float(max_retrace_ratio)) or (pullback10 > float(max_retrace_ratio)):
                    ok = False
            else:
                # 仍贴近低位 + 反弹不能太大
                if (pos72 > float(pos_threshold_long)) or (pos24 > float(pos_threshold_long)) or (pos10 > float(pos_threshold_long)):
                    ok = False
                if (rebound72 > float(max_retrace_ratio)) or (rebound24 > float(max_retrace_ratio)) or (rebound10 > float(max_retrace_ratio)):
                    ok = False

            if not ok:
                continue

            c.update({
                "pos_72h": pos72,
                "pos_24h": pos24,
                "pos_10d": pos10,
                "pullback_72h": pullback72,
                "pullback_24h": pullback24,
                "pullback_10d": pullback10,
                "rebound_72h": rebound72,
                "rebound_24h": rebound24,
                "rebound_10d": rebound10,
                "last_kline_1h": last,
                "low_72h": lo72, "high_72h": hi72,
                "low_10d": lo10, "high_10d": hi10,
            })
            results.append(c)

        results.sort(key=lambda x: float(x.get("abs_pct") or 0.0), reverse=True)
        results = results[: max(1, int(top_n))]

        self._top_reversal_cache[cache_key] = {"ts": now, "data": results}
        return results

    def list_tradeable_contracts(
        self,
        quote_asset: str = "USDT",
        contract_type: str = "PERPETUAL",
        status: str = "TRADING"
    ) -> List[str]:
        """列出可交易合约列表（USDT 合约为主）。"""
        try:
            if self.dry_run:
                return ["BTCUSDT", "ETHUSDT"]

            qa = (quote_asset or "USDT").upper()
            ct = (contract_type or "PERPETUAL").upper()
            st = (status or "TRADING").upper()

            info = self.client.futures_exchange_info()
            symbols: List[str] = []
            for s in (info.get("symbols") or []):
                try:
                    if st and (s.get("status") or "").upper() != st:
                        continue
                    if qa and (s.get("quoteAsset") or "").upper() != qa:
                        continue
                    if ct and (s.get("contractType") or "").upper() != ct:
                        continue
                    sym = s.get("symbol")
                    if sym:
                        symbols.append(sym)
                except Exception:
                    continue

            return sorted(list(dict.fromkeys(symbols)))

        except Exception as e:
            logger.error(f"获取可交易合约列表失败: {e}", exc_info=True)
            return []

    @staticmethod
    def _is_hammer_or_inverted(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按你最新定义识别锤子线/倒锤子线（不筛趋势、不要求实体贴边）。

        定义（只看几何比例）：
        - range = high - low
        - body  = |close - open|
        - upper = high - max(open, close)
        - lower = min(open, close) - low
        - long_wick  = max(upper, lower)
        - short_wick = min(upper, lower)

        条件：
        1) (body + short_wick) < range / 4
        2) long_wick > 3 * range / 4

        返回：
            pattern: "HAMMER" | "INVERTED_HAMMER"
            mode:    "long"（两者都可视作潜在看涨反转信号；区分请看 pattern）
            score:   long_wick / range（越大越“像”）
        """
        try:
            o = float(c.get("open"))
            h = float(c.get("high"))
            l = float(c.get("low"))
            cl = float(c.get("close"))
        except Exception:
            return None

        rng = h - l
        if rng <= 0:
            return None

        body = abs(cl - o)
        upper = h - max(o, cl)
        lower = min(o, cl) - l

        if upper < 0:
            upper = 0.0
        if lower < 0:
            lower = 0.0

        long_wick = max(upper, lower)
        short_wick = min(upper, lower)

        # ✅ 新规则：实体+短影线 < 1/4，总长影线 > 3/4
        if (body + short_wick) >= (rng / 4.0):
            return None
        if long_wick <= (3.0 * rng / 4.0):
            return None

        pattern = "HAMMER" if lower >= upper else "INVERTED_HAMMER"
        score = float(long_wick / max(rng, 1e-12))
        return {
            "pattern": pattern,
            "mode": "long",
            "score": score,
            "body": float(body),
            "range": float(rng),
            "upper_wick": float(upper),
            "lower_wick": float(lower),
            "long_wick": float(long_wick),
            "short_wick": float(short_wick),
        }

    def get_pinbar_pairs_usdt(
        self,
        interval: str = "1h",
        lookback_bars: int = 6,
        must_be_in_last_n: int = 2,
        volume_multiplier: float = 1.0,
        cache_ttl: int = 60,
    ) -> List[Dict[str, Any]]:
        """扫描所有 USDT 永续合约，找出满足条件的锤子线/倒锤子线交易对（不做趋势过滤）。

        条件：
        1) 取最近 lookback_bars 根 K 线；
        2) 形态必须出现在最近 must_be_in_last_n 根之一（默认最后 1 或倒数第 2 根）；
        3) 放量过滤：形态那根成交量 > 其余平均成交量 * volume_multiplier（默认 1.0）。

        返回：
            list[dict]，按 (volume_ratio, hammer_score) 排序。
        """
        now = time.time()

        cache_key = f"hammer_usdt_{interval}_{lookback_bars}_{must_be_in_last_n}_{volume_multiplier}"
        if not hasattr(self, "_pinbar_cache"):
            self._pinbar_cache = {}
        cached = self._pinbar_cache.get(cache_key)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) <= max(1, int(cache_ttl))):
            return cached.get("data") or []

        # 冷却期：不要打 REST（返回缓存或空）
        try:
            if self._is_in_rate_limit_cooldown():
                return cached.get("data") if cached else []
        except Exception:
            pass

        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        # 取可交易合约列表（USDT PERPETUAL）
        symbols: List[str] = []
        try:
            symbols = self.list_tradeable_contracts(quote_asset="USDT", contract_type="PERPETUAL", status="TRADING")
        except Exception:
            # 兜底：直接从 exchange_info 里过滤
            try:
                info = self.client.futures_exchange_info()
                for s in (info.get("symbols") or []):
                    if (s.get("status") or "").upper() != "TRADING":
                        continue
                    if (s.get("contractType") or "").upper() != "PERPETUAL":
                        continue
                    if (s.get("quoteAsset") or "").upper() != "USDT":
                        continue
                    sym = s.get("symbol")
                    if sym:
                        symbols.append(sym)
            except Exception:
                symbols = []

        results: List[Dict[str, Any]] = []

        # 统计（用于确认是否扫全/各阶段命中数量）
        total_symbols = 0
        ok_symbols = 0
        insufficient_klines = 0
        error_symbols = 0
        prelim_hammer_last2 = 0
        final_selected = 0

        for sym in (symbols or []):
            total_symbols += 1
            try:
                kl = self.get_kline(sym, interval=interval, limit=int(lookback_bars)) or []
                if len(kl) < int(lookback_bars):
                    insufficient_klines += 1
                    continue
                ok_symbols += 1

                # 初步统计：最近两根 (-1/-2) 是否出现锤子/倒锤（不看放量）
                prelim_hit = False
                for _i in (-1, -2):
                    try:
                        if self._is_hammer_or_inverted(kl[_i]):
                            prelim_hit = True
                            break
                    except Exception:
                        pass
                if prelim_hit:
                    prelim_hammer_last2 += 1

                # 仅允许出现在最近 must_be_in_last_n 根 + 放量过滤（复用统一实现，保证与 combo 扫描一致）
                r = self._eval_hammer_from_klines(
                    sym=sym,
                    kl=kl,
                    must_be_in_last_n=int(must_be_in_last_n),
                    volume_multiplier=float(volume_multiplier),
                )
                if not r:
                    continue

                results.append(r)
                final_selected += 1


            except Exception as e:
                error_symbols += 1
                try:
                    self._handle_rate_limit_error(e, context="get_pinbar_pairs_usdt:get_kline")
                except Exception:
                    pass
                continue

        # 排序：优先条件置顶 -> 放量 -> 同向K数量 -> 极值/锤长 -> 形态强度
        results.sort(
            key=lambda x: (
                int(x.get("priority") or 0),
                float(x.get("volume_ratio") or 0.0),
                int(x.get("same_dir_k_count") or 0),
                float(x.get("extreme_dist_ratio") or 0.0),
                float(x.get("hammer_score") or 0.0),
            ),
            reverse=True
        )

        logger.info(
            "🔎 HammerScan统计(不筛趋势): total=%s ok=%s insufficient_klines=%s errors=%s | prelim_hammer_last2=%s | final_selected=%s",
            total_symbols, ok_symbols, insufficient_klines, error_symbols, prelim_hammer_last2, final_selected
        )

        self._pinbar_cache[cache_key] = {"ts": now, "data": results}
        return results

    def _eval_hammer_from_klines(
        self,
        sym: str,
        kl: List[Dict[str, Any]],
        must_be_in_last_n: int,
        volume_multiplier: float,
    ) -> Optional[Dict[str, Any]]:
        """
        从已拉取的 klines 中评估“锤子线/倒锤子线 + 放量”是否命中。
        逻辑与 get_pinbar_pairs_usdt 内部一致：只允许出现在最近 must_be_in_last_n 根之一，
        并要求形态K的成交量 > 其余平均成交量 * volume_multiplier。
        """
        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        if not kl:
            return None

        found = None
        found_idx = None

        # 仅允许出现在最近 must_be_in_last_n 根
        for back in range(1, int(must_be_in_last_n) + 1):
            idx = -back
            info = self._is_hammer_or_inverted(kl[idx])
            if info:
                found = info
                found_idx = idx
                break

        if not found or found_idx is None:
            return None

        # 放量过滤：形态K的 volume > 其余平均 * multiplier
        vols = [_sf(k.get("volume")) for k in kl]
        pin_vol = vols[found_idx]
        # found_idx 是负数，下式与原实现一致：剔除形态K本身
        other_vols = [v for i, v in enumerate(vols) if i != (len(vols) + found_idx)]
        avg_other = (sum(other_vols) / len(other_vols)) if other_vols else 0.0
        if avg_other <= 0:
            return None

        volume_ratio = pin_vol / avg_other
        if volume_ratio <= float(volume_multiplier):
            return None
        
        # ===== 额外统计：同向K线数量 + 极值距离（用于排序/过滤）=====
        try:
            pin_k = kl[found_idx]
            pin_h = _sf(pin_k.get("high"))
            pin_l = _sf(pin_k.get("low"))
        except Exception:
            pin_h, pin_l = 0.0, 0.0

        # 长影线方向：倒锤子线=长影线向上；锤子线=长影线向下
        long_wick_up = str(found.get("pattern") or "").upper() == "INVERTED_HAMMER"

        # 最近6根（包含锤子线本身）
        window = kl[-6:] if len(kl) >= 6 else kl

        # 同向K数量：
        # 长影线向上 -> 统计上涨K（收>开）
        # 长影线向下 -> 统计下跌K（收<开）
        same_dir_k_count = 0
        for k in (window or []):
            o = _sf(k.get("open"))
            c = _sf(k.get("close"))
            if long_wick_up:
                if c > o:
                    same_dir_k_count += 1
            else:
                if c < o:
                    same_dir_k_count += 1

        # 最近6根极值
        min_low = min((_sf(k.get("low")) for k in (window or [])), default=0.0)
        max_high = max((_sf(k.get("high")) for k in (window or [])), default=0.0)

        # 锤子线长度（你原 hammer 识别里算过 range，这里直接用）
        hammer_len = float(found.get("range") or 0.0)

        if long_wick_up:
            # 长影线向上：看最低点到锤子线 low 的距离
            extreme_type = "min_low"
            extreme_price = float(min_low)
            extreme_dist = max(0.0, float(pin_l) - float(min_low)) if (pin_l > 0 and min_low > 0) else 0.0
        else:
            # 长影线向下：看最高点到锤子线 high 的距离
            extreme_type = "max_high"
            extreme_price = float(max_high)
            extreme_dist = max(0.0, float(max_high) - float(pin_h)) if (pin_h > 0 and max_high > 0) else 0.0

        extreme_dist_ratio = (float(extreme_dist) / float(hammer_len)) if hammer_len > 0 else 0.0

        # 优先标记：放量>1.5，同向K>=3，且极值距离 > 锤子线长度
        priority = 1 if (
            float(volume_ratio) > 1.5
            and int(same_dir_k_count) >= 4
            and hammer_len > 0
            and float(extreme_dist) > float(hammer_len)
        ) else 0
       
        return {
            "symbol": sym,
            "mode": str(found.get("mode") or "long"),
            "pinbar_index": int(found_idx),  # -1 / -2 / ...
            "pattern": str(found.get("pattern") or ""),
            "hammer_score": float(found.get("score") or 0.0),
            "volume_ratio": float(volume_ratio),
            "same_dir_k_count": int(same_dir_k_count),
            "extreme_type": str(extreme_type),
            "extreme_price": float(extreme_price),
            "extreme_dist": float(extreme_dist),
            "extreme_dist_ratio": float(extreme_dist_ratio),
            "hammer_len": float(hammer_len),
            "priority": int(priority),
        }

    def get_body_overlap_pairs_usdt(
        self,
        interval: str = "1h",
        lookback_bars: int = 6,          # 需要最近 6 根（最后2根 + 前4根）
        must_check_last_n: int = 2,       # 固定检查最后两根
        overlap_ratio: float = 0.80,      # 长实体 与 短实体 的重叠比例阈值（以“长实体”为分母）
        vol_boost: float = 1.30,          # 最近2根平均成交量 >= 前4根平均成交量 * 1.30
        cache_ttl: int = 60,
    ):
        """
        扫描所有 USDT 永续合约，筛选满足：
        1) 最新两根K线中，“较长实体”的80%以上与“较短实体”重叠；
        2) 最新两根平均成交量 >= 另外四根平均成交量 * 1.30。
        """

        import time
        now = time.time()

        cache_key = f"body_overlap_usdt_{interval}_{lookback_bars}_{overlap_ratio}_{vol_boost}"
        if not hasattr(self, "_body_overlap_cache"):
            self._body_overlap_cache = {}
        cached = self._body_overlap_cache.get(cache_key)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) <= max(1, int(cache_ttl))):
            return cached.get("data") or []

        # 冷却期：不要打 REST（返回缓存或空）
        try:
            if self._is_in_rate_limit_cooldown():
                return cached.get("data") if cached else []
        except Exception:
            pass

        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        def _body_range(c):
            o = _sf(c.get("open"))
            cl = _sf(c.get("close"))
            lo = min(o, cl)
            hi = max(o, cl)
            return lo, hi, abs(cl - o)

        def _overlap_len(a_lo, a_hi, b_lo, b_hi) -> float:
            return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))

        # 取可交易合约列表（USDT PERPETUAL）
        symbols = []
        try:
            symbols = self.list_tradeable_contracts(quote_asset="USDT", contract_type="PERPETUAL", status="TRADING")
        except Exception:
            symbols = []

        results = []

        for sym in (symbols or []):
            try:
                kl = self.get_kline(sym, interval=str(interval), limit=int(lookback_bars)) or []
                if len(kl) < int(lookback_bars):
                    continue

                # 最新两根
                c1 = kl[-2]
                c2 = kl[-1]

                a_lo, a_hi, a_body = _body_range(c1)
                b_lo, b_hi, b_body = _body_range(c2)

                # 避免十字星/极小实体导致除零
                if a_body <= 0 or b_body <= 0:
                    continue

                # 找出长实体与短实体
                if a_body >= b_body:
                    long_lo, long_hi, long_body = a_lo, a_hi, a_body
                    short_lo, short_hi, short_body = b_lo, b_hi, b_body
                else:
                    long_lo, long_hi, long_body = b_lo, b_hi, b_body
                    short_lo, short_hi, short_body = a_lo, a_hi, a_body

                ov = _overlap_len(long_lo, long_hi, short_lo, short_hi)
                # 关键：按你描述，“长实体”80%以上与短实体重叠 => overlap / long_body >= 0.8
                ov_ratio = ov / max(long_body, 1e-12)
                if ov_ratio < float(overlap_ratio):
                    continue

                # 成交量：最后2根 vs 前4根
                vols = [_sf(k.get("volume")) for k in kl]
                last2_avg = (vols[-1] + vols[-2]) / 2.0
                prev4 = vols[:-2]  # 前4根（lookback=6时刚好）
                prev4_avg = (sum(prev4) / len(prev4)) if prev4 else 0.0
                if prev4_avg <= 0:
                    continue

                vol_ratio = last2_avg / prev4_avg
                if vol_ratio < float(vol_boost):
                    continue

                results.append({
                    "symbol": sym,
                    "overlap_ratio": float(ov_ratio),
                    "vol_ratio": float(vol_ratio),
                    "last2_avg_vol": float(last2_avg),
                    "prev4_avg_vol": float(prev4_avg),
                })

            except Exception as e:
                try:
                    self._handle_rate_limit_error(e, context="get_body_overlap_pairs_usdt:get_kline")
                except Exception:
                    pass
                continue

        # 排序：优先放量，其次实体重叠
        results.sort(key=lambda x: (float(x.get("vol_ratio") or 0.0), float(x.get("overlap_ratio") or 0.0)), reverse=True)

        self._body_overlap_cache[cache_key] = {"ts": now, "data": results}
        return results

    def scan_hammer_and_overlap_pairs_usdt(
        self,
        interval: str = "1h",
        hammer_lookback_bars: int = 6,
        hammer_must_be_in_last_n: int = 2,
        hammer_volume_multiplier: float = 1.0,
        overlap_ratio: float = 0.80,
        vol_boost: float = 1.30,
        cache_ttl: int = 60,
    ):
        """
        一次拉K线，同时计算：
        - 锤子线/倒锤子线（与 get_pinbar_pairs_usdt 的判定一致）
        - 双K实体重叠 + 放量
        返回: {"hammer": [...], "overlap": [...]}
        """
        import time
        now = time.time()

        cache_key = (
            f"combo_scan_usdt_{interval}_{hammer_lookback_bars}_{hammer_must_be_in_last_n}_"
            f"{hammer_volume_multiplier}_{overlap_ratio}_{vol_boost}"
        )
        if not hasattr(self, "_combo_scan_cache"):
            self._combo_scan_cache = {}
        cached = self._combo_scan_cache.get(cache_key)
        if cached and (now - float(cached.get("ts", 0.0) or 0.0) <= max(1, int(cache_ttl))):
            return cached.get("data") or {"hammer": [], "overlap": []}

        # 冷却期：不要打 REST（返回缓存或空）
        try:
            if self._is_in_rate_limit_cooldown():
                return cached.get("data") if cached else {"hammer": [], "overlap": []}
        except Exception:
            pass

        def _sf(x, default=0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        def _body_range(c):
            o = _sf(c.get("open"))
            cl = _sf(c.get("close"))
            lo = min(o, cl)
            hi = max(o, cl)
            return lo, hi, abs(cl - o)

        def _overlap_len(a_lo, a_hi, b_lo, b_hi) -> float:
            return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))

        # 1) symbols 只取一次（USDT 永续）
        try:
            symbols = self.list_tradeable_contracts(
                quote_asset="USDT", contract_type="PERPETUAL", status="TRADING"
            )
        except Exception:
            symbols = []

        limit = max(int(hammer_lookback_bars), 6)

        hammer_results = []
        overlap_results = []

        for sym in (symbols or []):
            try:
                kl = self.get_kline(sym, interval=str(interval), limit=int(limit)) or []
                if len(kl) < 6:
                    continue

                # ========== A) 实体重叠 + 放量（用最后6根即可）==========
                last6 = kl[-6:]
                c1 = last6[-2]
                c2 = last6[-1]

                a_lo, a_hi, a_body = _body_range(c1)
                b_lo, b_hi, b_body = _body_range(c2)

                if a_body > 0 and b_body > 0:
                    if a_body >= b_body:
                        long_lo, long_hi, long_body = a_lo, a_hi, a_body
                        short_lo, short_hi = b_lo, b_hi
                    else:
                        long_lo, long_hi, long_body = b_lo, b_hi, b_body
                        short_lo, short_hi = a_lo, a_hi

                    ov = _overlap_len(long_lo, long_hi, short_lo, short_hi)
                    ov_ratio = ov / max(long_body, 1e-12)

                    vols6 = [_sf(x.get("volume")) for x in last6]
                    last2_avg = (vols6[-1] + vols6[-2]) / 2.0
                    prev4_avg = sum(vols6[:-2]) / 4.0
                    vol_ratio = (last2_avg / prev4_avg) if prev4_avg > 0 else 0.0

                    if ov_ratio >= float(overlap_ratio) and vol_ratio >= float(vol_boost):
                        overlap_results.append({
                            "symbol": sym,
                            "overlap_ratio": float(ov_ratio),
                            "vol_ratio": float(vol_ratio),
                            "last2_avg_vol": float(last2_avg),
                            "prev4_avg_vol": float(prev4_avg),
                        })

                # ========== B) 锤子线/倒锤子线（复用 get_pinbar 的逻辑）==========
                r = self._eval_hammer_from_klines(
                    sym=sym,
                    kl=kl,
                    must_be_in_last_n=int(hammer_must_be_in_last_n),
                    volume_multiplier=float(hammer_volume_multiplier),
                )
                if r:
                    hammer_results.append(r)

            except Exception as e:
                try:
                    self._handle_rate_limit_error(e, context="scan_hammer_and_overlap_pairs_usdt:get_kline")
                except Exception:
                    pass
                continue

        # 排序：hammer 与原 get_pinbar 一致；overlap 按放量+重叠
        hammer_results.sort(
            key=lambda x: (
                int(x.get("priority") or 0),
                float(x.get("volume_ratio") or 0.0),
                int(x.get("same_dir_k_count") or 0),
                float(x.get("extreme_dist_ratio") or 0.0),
                float(x.get("hammer_score") or 0.0),
            ),
            reverse=True
        )
        overlap_results.sort(
            key=lambda x: (float(x.get("vol_ratio") or 0.0), float(x.get("overlap_ratio") or 0.0)),
            reverse=True
        )

        data = {"hammer": hammer_results, "overlap": overlap_results}
        self._combo_scan_cache[cache_key] = {"ts": now, "data": data}
        return data
