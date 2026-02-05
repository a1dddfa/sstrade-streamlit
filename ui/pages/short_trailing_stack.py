# -*- coding: utf-8 -*-
"""
Short Trailing Stack UI page

Variant:
- Entry logic same as Short Trailing (STOP_LIMIT short at bid2).
- Stop protection: ONLY STOP_LIMIT trigger-limit orders, stacked on every price change.
"""
from __future__ import annotations

from typing import Optional
import streamlit as st
import logging

logger = logging.getLogger(__name__)

_IMPORT_ERROR: Optional[str] = None
try:
    from bots.short_trailing_stack.bot import ShortTrailingStackBot, ShortTrailingStackConfig
except Exception as e:  # pragma: no cover
    ShortTrailingStackBot = None  # type: ignore
    ShortTrailingStackConfig = None  # type: ignore
    _IMPORT_ERROR = repr(e)


def render() -> None:
    st.subheader("🪝 Short Trailing Stack（做空叠加止损）")

    exchange = st.session_state.get("exchange")
    if exchange is None:
        st.error("请先初始化交易所连接")
        return
    if _IMPORT_ERROR is not None:
        st.error(f"Bot 导入失败: {_IMPORT_ERROR}")
        return

    st.divider()
    st.subheader("配置")

    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input("交易对 Symbol", value="SOLUSDC").strip().upper().replace("/", "")
        qty = st.number_input("数量 Quantity", min_value=0.0001, max_value=100000.0, value=0.001, step=0.0001)
        stop_limit_distance = st.number_input(
            "止损距离（每次 tick 在市价上方 + 距离 挂一张 STOP_LIMIT）",
            min_value=0.00000001,
            max_value=1000000.0,
            value=0.1,
            step=0.01,
        )
    with col2:
        tag_prefix = st.text_input("标签前缀 Tag Prefix", value="UI_SHORTTRAILSTACK")
        st.caption("止损单将使用 tag: <Tag Prefix>_STOP_LIMIT；入场单使用 <Tag Prefix>_ENTRY。")

    st.divider()
    st.subheader("入场追价节流（可选，逻辑与原策略一致）")
    colm1, colm2, colm3 = st.columns(3)
    with colm1:
        entry_min_price_delta = st.number_input("最小追价变动", min_value=0.0, max_value=10000.0, value=0.5, step=0.1)
    with colm2:
        entry_min_replace_interval_sec = st.number_input("最小追价间隔（秒）", min_value=0.0, max_value=60.0, value=0.3, step=0.1)
    with colm3:
        entry_max_chase_sec = st.number_input("最大追价时长（秒，0=不限制）", min_value=0.0, max_value=3600.0, value=10.0, step=1.0)

    # Controls
    st.divider()
    st.subheader("控制")

    colb1, colb2, colb3 = st.columns(3)

    with colb1:
        if st.button("▶️ 启动 / 重启", key="ststack_start"):
            cfg = ShortTrailingStackConfig()
            cfg.symbol = symbol
            cfg.qty = float(qty)
            cfg.stop_limit_distance = float(stop_limit_distance)
            cfg.tag_prefix = tag_prefix
            cfg.entry_min_price_delta = float(entry_min_price_delta)
            cfg.entry_min_replace_interval_sec = float(entry_min_replace_interval_sec)
            cfg.entry_max_chase_sec = float(entry_max_chase_sec)

            # stop old
            old = st.session_state.get("short_trailing_stack_bot")
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass

            bot = ShortTrailingStackBot(exchange, cfg)
            st.session_state["short_trailing_stack_bot"] = bot
            try:
                bot.start()
                st.success("Short Trailing Stack Bot 已启动")
            except Exception as e:
                st.error(f"启动失败: {e}")

    with colb2:
        if st.button("⏹ 停止", key="ststack_stop"):
            bot = st.session_state.get("short_trailing_stack_bot")
            if bot is None:
                st.info("未运行")
            else:
                try:
                    bot.stop()
                    st.session_state.pop("short_trailing_stack_bot", None)
                    st.success("已停止")
                except Exception as e:
                    st.error(f"停止失败: {e}")

    with colb3:
        if st.button("🧹 取消当前交易对所有挂单", key="ststack_cancel_all"):
            try:
                exchange.cancel_all_orders(symbol=symbol)
                st.success("已取消全部挂单")
            except Exception as e:
                st.error(f"取消失败: {e}")

    # Status
    st.divider()
    st.subheader("状态")

    bot = st.session_state.get("short_trailing_stack_bot")
    if bot is None:
        st.info("未启动")
        return

    state = bot.logic.state
    st.write(f"**交易对:** {bot.cfg.symbol}")
    st.write(f"**数量:** {bot.cfg.qty}")
    st.write(f"**当前状态:** {'开仓中' if state.position_open else '等待入场'}")

    if state.position_open:
        st.write(f"**入场价格:** {state.entry_fill_price}")
        st.write(f"**最近一次 tick 价格:** {state.last_price_seen}")
        n = len(state.stop_limit_order_ids or [])
        st.write(f"**累计挂出的 STOP_LIMIT 止损单数:** {n}")
        if state.last_price_seen is not None:
            st.write(f"**本 tick 预期挂单价:** {float(state.last_price_seen) + bot.cfg.stop_limit_distance}")
    else:
        st.write(f"**入场挂单价（bid2）:** {state.entry_price}")
