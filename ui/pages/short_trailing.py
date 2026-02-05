# -*- coding: utf-8 -*-
"""
Short Trailing UI page
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

import streamlit as st

import logging

logger = logging.getLogger(__name__)

# Bots should be importable from your project
_SHORT_TRAILING_IMPORT_ERROR: Optional[str] = None


try:
    from bots.short_trailing.bot import ShortTrailingBot, ShortTrailingConfig
    from bots.short_trailing.logic import ShortTrailingState
except Exception as e:  # pragma: no cover
    ShortTrailingBot = None  # type: ignore
    ShortTrailingConfig = None  # type: ignore
    ShortTrailingState = None  # type: ignore
    _SHORT_TRAILING_IMPORT_ERROR = repr(e)


def _resolve_from_main(name: str):
    import sys
    main = sys.modules.get("__main__")
    if main is None or not hasattr(main, name):
        raise RuntimeError(
            f"short_trailing.py expected `{name}` to exist in the Streamlit main script "
            f"(__main__). Keep it in streamlit_app.py for now."
        )
    return getattr(main, name)


def render() -> None:
    st.subheader("🪝 Short Trailing（做空跟踪止损）")

    exchange = st.session_state.get("exchange")

    if exchange is None:
        st.error("请先初始化交易所连接")
        return

    if _SHORT_TRAILING_IMPORT_ERROR is not None:
        st.error(f"Bot 导入失败: {_SHORT_TRAILING_IMPORT_ERROR}")
        return

    # Configuration
    st.divider()
    st.subheader("配置")

    def _get_last_price(sym: str) -> Optional[float]:
        """Best-effort REST ticker read for the current market price."""
        try:
            t = exchange.get_ticker(sym)
        except Exception as e:
            # 记录真实异常，便于定位“启动时拿不到 ticker”问题
            logger.exception(f"启动读取ticker失败: symbol={sym}: {e}")
            st.session_state["last_ticker_error"] = str(e)
            return None
        if not isinstance(t, dict):
            return None
        px = t.get("price") or t.get("last") or t.get("lastPrice") or t.get("c")
        if px is None:
            return None
        try:
            return float(px)
        except Exception:
            return None

    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input("交易对 Symbol", value="SOLUSDC")
        qty = st.number_input("数量 Quantity", min_value=0.0001, max_value=100.0, value=0.001, step=0.0001)
        entry_distance = st.number_input(
            "启动入场距离（与市价距离，做空=市价+距离）",
            min_value=0.01,
            max_value=1000000.0,
            value=0.1,
            step=1.0,
        )
        stop_limit_distance = st.number_input("止损限价距离", min_value=0.01, max_value=1000.0, value=0.01, step=1.0)
        stop_market_extra_distance = st.number_input("止损市价额外距离", min_value=0.01, max_value=1000.0, value=0.01, step=1.0)
    with col2:
        next_entry_distance = st.number_input("下一轮入场距离", min_value=0.01, max_value=1000.0, value=0.01, step=1.0)
        cancel_distance = st.number_input("取消距离", min_value=0.1, max_value=1000.0, value=1.0, step=1.0)
        reentry_distance = st.number_input("重新入场距离", min_value=0.01, max_value=1000.0, value=0.01, step=1.0)
        tag_prefix = st.text_input("标签前缀 Tag Prefix", value="UI_SHORTTRAIL")

    # Entry (maker-only) controls
    st.divider()
    st.subheader("入场（Maker Only）")
    entry_maker_only = st.checkbox("启动入场只做 Maker（Post-Only），并自动追价重挂", value=True)
    colm1, colm2, colm3 = st.columns(3)
    with colm1:
        entry_min_price_delta = st.number_input(
            "最小追价变动（价格变化>=此值才重挂）",
            min_value=0.0,
            max_value=1000.0,
            value=0.1,
            step=0.1,
        )
    with colm2:
        entry_min_replace_interval_sec = st.number_input(
            "最小重挂间隔（秒）",
            min_value=0.05,
            max_value=5.0,
            value=0.2,
            step=0.05,
        )
    with colm3:
        entry_max_chase_sec = st.number_input(
            "最大追价时长（秒，0=不限制）",
            min_value=0.0,
            max_value=120.0,
            value=0.0,
            step=1.0,
        )

    # Throttle controls
    st.divider()
    st.subheader("节流控制")
    col3, col4 = st.columns(2)
    with col3:
        min_stop_price_delta = st.number_input("最小止损价格变化", min_value=0.0, max_value=100.0, value=0.01, step=0.01)
    with col4:
        min_replace_interval_sec = st.number_input("最小更新间隔 (秒)", min_value=0.1, max_value=5.0, value=0.2, step=0.1)

    # Create config
    cfg = ShortTrailingConfig()
    cfg.symbol = symbol
    cfg.qty = qty
    cfg.stop_limit_distance = stop_limit_distance
    cfg.stop_market_extra_distance = stop_market_extra_distance
    cfg.next_entry_distance = next_entry_distance
    cfg.cancel_distance = cancel_distance
    cfg.reentry_distance = reentry_distance
    cfg.entry_maker_only = entry_maker_only
    cfg.entry_min_price_delta = entry_min_price_delta
    cfg.entry_min_replace_interval_sec = entry_min_replace_interval_sec
    cfg.entry_max_chase_sec = entry_max_chase_sec
    cfg.tag_prefix = tag_prefix
    cfg.min_stop_price_delta = min_stop_price_delta
    cfg.min_replace_interval_sec = min_replace_interval_sec

    # Bot controls
    st.divider()
    st.subheader("控制")
    
    col5, col6 = st.columns(2)
    with col5:
        if st.button("启动 Bot", key="start_short_trailing_bot"):
            if "short_trailing_bot" in st.session_state:
                try:
                    st.session_state.short_trailing_bot.stop()
                except Exception:
                    pass

            bot = ShortTrailingBot(exchange, cfg)
            st.session_state.short_trailing_bot = bot

            # ✅ 关键：把 bot 注册到 user stream dispatcher
            # 否则 WS 线程里的订单更新（FILLED 等）不会转发到 bot，
            # 就不会触发 on_entry_filled -> 挂出止损单（也就无法"移动止损"）
            try:
                _get_user_stream_dispatcher = _resolve_from_main("_get_user_stream_dispatcher")
                dispatcher = _get_user_stream_dispatcher()
                if hasattr(dispatcher, "register_order_consumer"):
                    dispatcher.register_order_consumer(bot)
                if hasattr(dispatcher, "register_ws_event_consumer"):
                    dispatcher.register_ws_event_consumer(bot)
            except Exception as e:
                # 不中断启动，但给 UI 明确提示，便于排查
                st.warning(
                    f"⚠️ 未能注册到 UserStreamDispatcher（可能导致无法自动挂止损/移动止损）：{e}"
                )

            bot.start()
            try:
                # Entry: trigger on bid2
                bot.logic.place_entry_trigger_bid2()
            except Exception as e:
                try:
                    bot.stop()
                except Exception:
                    pass
                st.error(f"入场挂单失败: {e}")
                return
            st.success("Short Trailing Bot 已启动")
    
    with col6:
        if st.button("停止 Bot", key="stop_short_trailing_bot"):
            if "short_trailing_bot" in st.session_state:
                try:
                    st.session_state.short_trailing_bot.stop()
                    st.success("Short Trailing Bot 已停止")
                except Exception as e:
                    st.error(f"停止失败: {e}")

    # Status display
    st.divider()
    st.subheader("状态")
    
    if "short_trailing_bot" in st.session_state:
        bot = st.session_state.short_trailing_bot
        logic = bot.logic
        state = logic.state
        
        st.write(f"**交易对:** {bot.cfg.symbol}")
        st.write(f"**数量:** {bot.cfg.qty}")
        st.write(f"**当前状态:** {'开仓中' if state.position_open else '等待入场'}")
        
        if state.position_open:
            st.write(f"**入场价格:** {state.entry_fill_price}")
            st.write(f"**当前最低价:** {state.lowest_price}")
            st.write(f"**预期止损限价:** {state.lowest_price + bot.cfg.stop_limit_distance}")
            st.write(f"**预期止损市价:** {state.lowest_price + bot.cfg.stop_limit_distance + bot.cfg.stop_market_extra_distance}")
        else:
            st.write(f"**当前入场单价格:** {state.entry_price if state.entry_price else '无'}")
            st.write(f"**入场单ID:** {state.entry_order_id if state.entry_order_id else '无'}")
    else:
        st.write("Bot 未启动")

    # Import error display
    if _SHORT_TRAILING_IMPORT_ERROR:
        st.divider()
        st.error(f"导入错误: {_SHORT_TRAILING_IMPORT_ERROR}")
        st.info("请检查 bots/short_trailing 模块是否正确安装")
