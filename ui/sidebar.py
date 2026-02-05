# -*- coding: utf-8 -*-
"""
Sidebar UI for Streamlit Trading Control Panel.

Goal (step-1 refactor):
- Move ONLY the sidebar rendering + "init/reconnect" button logic out of streamlit_app.py
- Keep behavior identical by dynamically resolving dependencies from the Streamlit main script
  (i.e., the file you run via `streamlit run ...`).

Next steps (recommended):
- Replace dynamic dependency resolution with proper imports from services/ (exchange_service, user_stream_service).
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from services.exchange_service import init_exchange_flow

# Import bots for strategy selection
try:
    from bots.ladder.bot import LadderBot  # type: ignore
except Exception:
    LadderBot = None  # type: ignore

try:
    from bots.range_two.bot import RangeTwoBot  # type: ignore
except Exception:
    RangeTwoBot = None  # type: ignore

try:
    from bots.short_trailing.bot import ShortTrailingBot  # type: ignore
except Exception:
    ShortTrailingBot = None  # type: ignore

try:
    from bots.short_trailing_stack.bot import ShortTrailingStackBot  # type: ignore
except Exception:
    ShortTrailingStackBot = None  # type: ignore

STRATEGIES = {
    "Ladder": LadderBot,
    "Range Two": RangeTwoBot,
    "Short Trailing": ShortTrailingBot,  # ★ 新增
    "Short Trailing Stack": ShortTrailingStackBot,
}


def _resolve_from_main(name: str):
    """Resolve a symbol from the Streamlit main script module (__main__)."""
    import sys

    main = sys.modules.get("__main__")
    if main is None or not hasattr(main, name):
        raise RuntimeError(
            f"sidebar.py expected `{name}` to exist in the Streamlit main script. "
            f"Please keep `{name}` in streamlit_app.py for now, or pass a callback."
        )
    return getattr(main, name)


_PAGE_LABELS = {
    "hammer": "🔨 锤子线扫描",
    "ladder": "🧩 阶梯 + 手动下单",
    "short_trailing": "🪝 Short Trailing（做空跟踪止损）",
    "short_trailing_stack": "🪝 Short Trailing Stack（做空叠加止损）",
    "logs": "🧾 日志",
    "account": "📊 账户",
}


def render_sidebar(
    *,
    cfg_path_default: str = "config.yaml",
    dry_run_default: bool = False,
) -> str:
    """
    Render the sidebar and return the selected page string (same as old code).

    This function preserves the original behavior:
    - config path input
    - dry_run toggle
    - page radio
    - init/reconnect button that:
        - loads config
        - disconnects old exchange WS
        - creates new exchange
        - rebinds bots
        - subscribes user-stream once and registers dispatcher targets

    NOTE: For step-1 refactor, we keep all side effects here to avoid changing runtime behavior.
    """
    with st.sidebar:
        st.header("连接设置")
        cfg_path = st.text_input("config.yaml 路径", value=cfg_path_default)
        override_dry_run = st.toggle("dry_run（模拟下单）", value=dry_run_default)

        # ==============================
        # Health indicator (WS/REST/Degraded unified)
        # ==============================
        ex = st.session_state.get("exchange")
        if ex is not None and hasattr(ex, "get_connection_health"):
            try:
                h = ex.get_connection_health()
                ws = h.get("ws")
                rest = h.get("rest")
                overall = h.get("overall")

                def _emoji(s: str) -> str:
                    if s == "ok":
                        return "🟢"
                    if s == "degraded":
                        return "🟡"
                    return "🔴"

                st.markdown(
                    """
                    #### 连接健康
                    - **WS**: {ws_e}
                    - **REST**: {rest_e}
                    - **Overall**: {ov_e}
                    """.format(
                        ws_e=f"{_emoji(ws)} {ws}",
                        rest_e=f"{_emoji(rest)} {rest}",
                        ov_e=f"{_emoji(overall)} {overall}",
                    )
                )
                if h.get("user_event_age_min") is not None:
                    st.caption(f"last user event age: {h.get('user_event_age_min')} min")
            except Exception:
                # health UI is best-effort
                pass

        st.divider()
        st.header("页面")
        page_key = st.radio(
            "选择功能页",
            options=["hammer", "ladder", "short_trailing", "short_trailing_stack", "logs", "account"],
            format_func=lambda k: _PAGE_LABELS.get(k, str(k)),
            key="page_select",
        )

        if st.button("🔌 初始化 / 重新连接", key="init_exchange"):
            init_exchange_flow(cfg_path, override_dry_run=override_dry_run)

    return page_key
