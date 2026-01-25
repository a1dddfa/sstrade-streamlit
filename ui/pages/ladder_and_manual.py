# -*- coding: utf-8 -*-
"""
Ladder + Manual order page extracted from streamlit_app.py (step-4 refactor).

Transitional design:
- Keep UI + logic identical.
- Resolve certain legacy globals from the Streamlit main script (__main__) to avoid
  forcing a full refactor in one step.

Expected legacy symbols in streamlit_app.py (for now):
- _get_user_stream_dispatcher()
- logger (UILogger stored in st.session_state["logger"])
- LadderConfig, RangeTwoConfig (if not importable elsewhere)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

import pandas as pd
import streamlit as st

# Bots should be importable from your project
_LADDER_IMPORT_ERROR: Optional[str] = None
_RANGE2_IMPORT_ERROR: Optional[str] = None

try:
    from bots.ladder.bot import LadderBot  # type: ignore
except Exception as e:  # pragma: no cover
    LadderBot = None  # type: ignore
    _LADDER_IMPORT_ERROR = repr(e)

try:
    from bots.range_two.bot import RangeTwoBot  # type: ignore
except Exception as e:  # pragma: no cover
    RangeTwoBot = None  # type: ignore
    _RANGE2_IMPORT_ERROR = repr(e)


def _resolve_from_main(name: str):
    import sys
    main = sys.modules.get("__main__")
    if main is None or not hasattr(main, name):
        raise RuntimeError(
            f"ladder_and_manual.py expected `{name}` to exist in the Streamlit main script "
            f"(__main__). Keep it in streamlit_app.py for now."
        )
    return getattr(main, name)


def _get_config_classes():
    """
    Try to get LadderConfig / RangeTwoConfig in a safe way.
    Priority:
      1) resolve from __main__
      2) resolve from bot modules (common patterns)
    """
    LadderConfig = None
    RangeTwoConfig = None
    try:
        LadderConfig = _resolve_from_main("LadderConfig")
    except Exception:
        pass
    try:
        RangeTwoConfig = _resolve_from_main("RangeTwoConfig")
    except Exception:
        pass

    if LadderConfig is None and LadderBot is not None:
        LadderConfig = getattr(__import__("bots.ladder.bot", fromlist=["LadderConfig"]), "LadderConfig", None)
    if RangeTwoConfig is None and RangeTwoBot is not None:
        RangeTwoConfig = getattr(__import__("bots.range_two.bot", fromlist=["RangeTwoConfig"]), "RangeTwoConfig", None)

    if LadderConfig is None or RangeTwoConfig is None:
        raise RuntimeError("Cannot resolve LadderConfig/RangeTwoConfig. Keep them in streamlit_app.py for now.")
    return LadderConfig, RangeTwoConfig


def render() -> None:
    st.subheader("手动交易面板：阶梯策略 + 手动下单（Hedge / LONG+SHORT）")

    exchange = st.session_state.get("exchange")
    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
        return

    # If bot imports failed, show the real reason early (otherwise you only see a confusing config error later).
    if LadderBot is None and _LADDER_IMPORT_ERROR:
        st.error(f"❌ 导入 LadderBot 失败：{_LADDER_IMPORT_ERROR}")
        return
    if RangeTwoBot is None and _RANGE2_IMPORT_ERROR:
        st.error(f"❌ 导入 RangeTwoBot 失败：{_RANGE2_IMPORT_ERROR}")
        return

    _get_user_stream_dispatcher = _resolve_from_main("_get_user_stream_dispatcher")
    dispatcher = _get_user_stream_dispatcher()

    # UILogger is stored in session by original code
    ui_logger = st.session_state.get("logger") or getattr(_resolve_from_main("st"), "session_state", {}).get("logger", None)

    LadderConfig, RangeTwoConfig = _get_config_classes()

    # -----------------------------
    # Ladder bot
    # -----------------------------
    if "ladder_bot" not in st.session_state:
        if LadderBot is None:
            raise RuntimeError("LadderBot import failed. Ensure bots.ladder.bot exists.")
        st.session_state["ladder_bot"] = LadderBot(exchange, ui_logger)
    bot = st.session_state["ladder_bot"]
    bot.exchange = exchange

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

    # -----------------------------
    # RangeTwo bot
    # -----------------------------
    st.markdown("### 🎯 区间两单策略（A1/A2 + 固定TP/SL + 条件补挂保本止损）")

    if "range2_bot" not in st.session_state:
        if RangeTwoBot is None:
            raise RuntimeError("RangeTwoBot import failed. Ensure bots.range_two.bot exists.")
        st.session_state["range2_bot"] = RangeTwoBot(exchange, ui_logger)
    rbot = st.session_state["range2_bot"]
    rbot.exchange = exchange

    # register to WS dispatcher for fill events
    try:
        dispatcher.register_range2_bot(rbot)
    except Exception:
        pass

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

    # preview
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
                rbot.start()
                st.success("已发送 A1/A2，并已自动启动监控（回执见日志）")
            except Exception as e:
                st.error(f"下单失败：{e}")
                try:
                    ui_logger.log(f"❌ 区间两单下单失败：{e}")
                except Exception:
                    pass
    with rcol3:
        if st.button("🚀 启动监控", key="r2_start"):
            rbot.start()
    with rcol4:
        if st.button("🛑 停止监控", key="r2_stop"):
            rbot.stop()

    st.caption("说明：A1/A2 都会自动带固定 TP/SL；监控只负责在满足条件后补挂 closePosition 的 STOP_MARKET（全部止损）。建议先 dry_run 测试。")
    st.json(asdict(rbot.state), expanded=False)

    st.divider()

    # -----------------------------
    # Manual order panel
    # -----------------------------
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

    # mark price (hint only)
    mark = None
    try:
        t = exchange.get_ticker(sym2) or {}
        mark = float(t.get("lastPrice") or t.get("markPrice") or 0.0) or None
    except Exception:
        mark = None
    st.caption(f"当前价(参考)：{mark}" if mark else "当前价：获取失败（不影响你手动输入价格下单）")

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

    use_deferred_stoplimit = False
    dsl_arm_price = dsl_limit_price = None
    dsl_use_pos_qty = True
    if enable_sl and auto_protection:
        use_deferred_stoplimit = st.toggle("延迟挂 StopLimit 止损（到达启用价后才下 STOP_LIMIT）", value=False)
        if use_deferred_stoplimit:
            d1, d2, d3 = st.columns([1, 1, 1])
            with d1:
                dsl_arm_price = st.number_input("启用价(armPrice)：到这个价才开始挂 StopLimit", min_value=0.0, value=0.0, step=0.01, format="%.6f")
            with d2:
                dsl_limit_price = st.number_input("StopLimit 限价(limitPrice)：挂出的限价", min_value=0.0, value=float(sl_price) if sl_price else 0.0, step=0.01, format="%.6f")
            with d3:
                dsl_use_pos_qty = st.toggle("用触发时仓位量(推荐)", value=True)
            st.caption("说明：armPrice 到达后才会提交真正的 STOP_LIMIT（带 stopPrice=SL 触发价 + limitPrice）。StopLimit 需要 quantity；勾选后会在触发时自动用当前仓位量。")

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
                    "positionSide": position_side,
                }

                if auto_protection:
                    if enable_tp and tp_price and tp_price > 0:
                        params["take_profit"] = {"price": float(tp_price)}
                    if enable_sl and sl_price and sl_price > 0:
                        if not use_deferred_stoplimit:
                            params["stop_loss"] = {"price": float(sl_price)}

                o = exchange.create_order(
                    symbol=sym2.strip().upper().replace("/", ""),
                    side=order_side,
                    order_type=order_type,
                    quantity=float(qty),
                    price=float(price) if (order_type == "limit" and price and price > 0) else None,
                    params=params,
                )

                if auto_protection and enable_sl and use_deferred_stoplimit:
                    if not dsl_arm_price or float(dsl_arm_price) <= 0:
                        raise ValueError("启用两段式 StopLimit 时，armPrice 必须 > 0")
                    if not dsl_limit_price or float(dsl_limit_price) <= 0:
                        raise ValueError("启用两段式 StopLimit 时，limitPrice 必须 > 0")
                    dsl_qty = 0.0 if dsl_use_pos_qty else float(qty)
                    dsl = exchange.create_order(
                        symbol=sym2.strip().upper().replace("/", ""),
                        side=("short" if order_side == "long" else "long"),
                        order_type="deferred_stop_limit",
                        quantity=float(dsl_qty),
                        price=float(dsl_limit_price),
                        params={
                            "tag": "MANUAL_DSL",
                            "positionSide": position_side,
                            "activatePrice": float(dsl_arm_price),
                            "stopPrice": float(sl_price),
                            "limitPrice": float(dsl_limit_price),
                        },
                    )
                    try:
                        ui_logger.log(f"🧷 已注册两段式 StopLimit：{dsl}")
                    except Exception:
                        pass

                try:
                    ui_logger.log(f"✅ 手动下单成功：{o}")
                except Exception:
                    pass
                st.success("下单已发送（回执见日志）")
            except Exception as e:
                st.error(f"下单失败：{e}")
                try:
                    ui_logger.log(f"❌ 手动下单失败：{e}")
                except Exception:
                    pass

    with colO2:
        st.caption("建议先勾选 dry_run 测试；实盘前务必确认：合约类型、最小下单量、杠杆、保证金模式、Hedge 模式。")

    st.divider()

    # -----------------------------
    # Positions
    # -----------------------------
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
