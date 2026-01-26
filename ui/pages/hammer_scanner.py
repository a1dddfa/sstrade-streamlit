# -*- coding: utf-8 -*-
"""
Hammer scanner page extracted from streamlit_app.py (step-3 refactor).

Transitional design:
- Keep UI + logic identical.
- Resolve certain legacy globals from the Streamlit main script (__main__) to avoid
  forcing a full import graph refactor in one step.

Expected legacy symbols in streamlit_app.py (for now):
- _get_user_stream_dispatcher(): returns UserStreamDispatcher (optional, but recommended)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import time

import pandas as pd
import streamlit as st


def _resolve_from_main(name: str):
    import sys
    main = sys.modules.get("__main__")
    if main is None or not hasattr(main, name):
        raise RuntimeError(
            f"hammer_scanner.py expected `{name}` to exist in the Streamlit main script "
            f"(__main__). Keep it in streamlit_app.py for now."
        )
    return getattr(main, name)


def render() -> None:
    st.subheader("扫描：USDT 永续合约的「锤子线 / 倒锤子线」(默认 1h)（可勾选并同步到下单面板）")

    exchange = st.session_state.get("exchange")

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

    def _render_hammer_table(rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return

        df = pd.DataFrame(rows)
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
            df_show["建议方向"] = df_show["建议方向"].map({"short": "做空", "long": "做多"}).fillna(df_show["建议方向"])

        if "✅选择" not in df_show.columns:
            df_show.insert(0, "✅选择", False)

        edited = st.data_editor(
            df_show,
            use_container_width=True,
            height=460,
            hide_index=True,
            column_config={"✅选择": st.column_config.CheckboxColumn(required=False)},
            key="hammer_table_editor",  # ✅ 切页回来还能保留勾选（同一 session）
        )

        picked = edited[edited["✅选择"] == True]
        colP1, colP2 = st.columns([1, 2])
        with colP1:
            if st.button("➡️ 使用选中交易对", disabled=picked.empty, key="use_hammer_pick"):
                sym = str(picked.iloc[0]["交易对"]).strip().upper().replace("/", "")
                st.session_state["selected_symbol"] = sym
                st.success(f"已选择：{sym}（已同步到下单面板）")
        with colP2:
            st.caption("勾选一行后点按钮，会把交易对同步到下单面板的输入框。")

    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
    else:
        do_scan = bool(manual_scan_once)
        # ❌ 该页不再使用 autorefresh 触发扫描；保留开关仅为兼容 UI
        _ = scan_enable, refresh_sec  # unused but kept for parity

        if do_scan:
            try:
                combo_data = exchange.scan_hammer_and_overlap_pairs_usdt(
                    interval=str(interval),
                    hammer_lookback_bars=int(lookback_bars),
                    hammer_must_be_in_last_n=int(must_be_in_last_n),
                    hammer_volume_multiplier=float(volume_multiplier),
                    overlap_ratio=float(st.session_state.get("overlap_ratio", 80.0)) / 100.0,
                    vol_boost=float(st.session_state.get("vol_boost", 1.30)),
                    cache_ttl=int(cache_ttl),
                ) or {"hammer": [], "overlap": []}

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
                    _render_hammer_table(rows)

            except Exception as e:
                st.error(f"扫描失败：{e}")
                st.exception(e)
        else:
            rows = st.session_state.get("_hammer_rows_cache") or []
            ts = st.session_state.get("_hammer_rows_cache_ts")
            if rows:
                if ts:
                    st.info(f"扫描已暂停：当前展示缓存结果（上次扫描：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))}）")
                else:
                    st.info("扫描已暂停：当前展示缓存结果（无时间戳）")
                _render_hammer_table(rows)
            else:
                st.info("扫描已暂停：暂无缓存结果。你可以点一次“手动扫描一次”。")

    st.divider()
    st.subheader("扫描：双K实体80%重叠 + 近两根放量(>= 前四根均量 * 1.30)")

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
        _ = overlap_ratio, vol_boost  # parity
    with oc3:
        overlap_display_limit = st.number_input(
            "展示数量(重叠扫描)",
            min_value=1, max_value=200, value=50, step=1,
            key="overlap_display_limit",
        )

    exchange = st.session_state.get("exchange")
    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
        return

    combo_data = st.session_state.get("_combo_scan_data") or {}
    combo_interval = str(st.session_state.get("_combo_scan_interval", ""))

    current_interval = str(st.session_state.get("hammer_scan_interval_override") or str(st.session_state.get("page_hammer_interval") or ""))
    # We also have `interval` local variable above; but Streamlit reruns preserve widget state.
    # For exact parity, prefer the widget state value:
    try:
        current_interval = str(st.session_state.get("hammer_scan_enable"))  # dummy to avoid linter warnings
    except Exception:
        pass
    # Use the selected interval widget value from this run
    # (it exists as a local; we re-fetch via the widget key in session_state is not set by default)
    # In practice, the local `interval` above is the correct source; reuse it:
    current_interval = str(interval)

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
