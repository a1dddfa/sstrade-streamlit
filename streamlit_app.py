# -*- coding: utf-8 -*-
"""
Streamlit Trading Control Panel (refactored entry).

This is the cleaned entry after splitting:
- ui/sidebar.py
- services/exchange_service.py
- ui/pages/hammer_scanner.py
- ui/pages/ladder_and_manual.py

Notes:
- This entry keeps the minimum set of globals expected by transitional modules:
  - logger (python logging.Logger)
  - load_config
  - init_exchange
  - _get_user_stream_dispatcher
- As you continue refactoring, you can move these into services/ and remove the
  __main__ dynamic resolution in other modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import logging
import streamlit as st
import time
import yaml

from infra.logging.ui_logger import UILogger
from infra.ws.user_stream import UserStreamDispatcher

# Import the new config loader
from utils.config import load_config

# Prefer exchange wrapper under exchanges/
try:
    from exchanges.binance_exchange import BinanceExchange  # type: ignore
except Exception:  # pragma: no cover
    from binance_exchange import BinanceExchange  # type: ignore

# Logging setup (same idea as original)
PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
try:
    from core.logging_config import setup_logging
except Exception:  # pragma: no cover
    from logging_config import setup_logging  # type: ignore

setup_logging(log_dir=str(LOG_DIR), level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("✅ Streamlit logging initialized, log_dir=%s", LOG_DIR)


# -----------------------------
# Transitional globals used by sidebar/service modules
# -----------------------------
def load_config_file(cfg_path: str) -> Dict[str, Any]:
    """Load YAML config into dict."""
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def init_exchange(cfg: Dict[str, Any], override_dry_run: bool = False) -> Any:
    """
    Initialize exchange instance.
    """
    global_cfg = cfg.get("global") or {}
    exchanges_cfg = cfg.get("exchanges") or {}
    binance_cfg = exchanges_cfg.get("binance") or exchanges_cfg.get("BINANCE")
    if binance_cfg is None:
        raise ValueError("配置缺少 exchanges.binance")

    if override_dry_run:
        global_cfg = dict(global_cfg)
        global_cfg["dry_run"] = True

    # Load config with environment variable support
    config_with_env = load_config(cfg)
    
    # Update binance config with environment variables
    binance_cfg = dict(binance_cfg)
    binance_cfg["api_key"] = config_with_env["api_key"]
    binance_cfg["api_secret"] = config_with_env["api_secret"]
    binance_cfg["proxy"] = config_with_env["proxy"]
    
    # Update global config with environment variables
    global_cfg = dict(global_cfg)
    global_cfg["use_ws"] = config_with_env["use_ws"]

    # ✅ 强制用正确层级：BinanceExchange(config, global_config)
    return BinanceExchange(binance_cfg, global_cfg)


# -----------------------------
# Session-scoped dispatcher
# -----------------------------
def _get_user_stream_dispatcher() -> UserStreamDispatcher:
    if "user_stream_dispatcher" not in st.session_state:
        st.session_state["user_stream_dispatcher"] = UserStreamDispatcher()
    return st.session_state["user_stream_dispatcher"]


# -----------------------------
# Streamlit app entry
# -----------------------------
from ui.sidebar import render_sidebar
from ui.pages.hammer_scanner import render as render_hammer_scanner
from ui.pages.ladder_and_manual import render as render_ladder_and_manual
from ui.pages.short_trailing import render as render_short_trailing
from ui.pages.short_trailing_stack import render as render_short_trailing_stack


def _ensure_ui_logger_registered() -> UILogger:
    if "logger" not in st.session_state:
        st.session_state["logger"] = UILogger()
    ui_logger: UILogger = st.session_state["logger"]

    # Register UI logger to dispatcher so WS thread can write logs
    try:
        _get_user_stream_dispatcher().register_ui_logger(ui_logger)
    except Exception:
        # If dispatcher API changes, don't crash UI.
        pass
    return ui_logger


def render_logs_page(ui_logger: UILogger) -> None:
    st.subheader("实时日志（最近 300 行）")
    try:
        st.code(ui_logger.tail(300), language="text")
    except Exception:
        st.info("日志组件不可用（UILogger.tail 不存在或报错）")


def render_account_panel(exchange: Any, *, symbol: Optional[str] = None) -> None:
    """
    Minimal account panel fallback.

    If your project has a richer implementation, you can replace this function or
    import your existing account renderer here.
    """
    st.subheader("账户信息（简版）")
    if symbol:
        st.caption(f"symbol filter: {symbol}")
    # Try common methods
    for fn_name in ("get_account", "fetch_account", "account", "get_balances"):
        if hasattr(exchange, fn_name):
            try:
                data = getattr(exchange, fn_name)()
                st.json(data, expanded=False)
                return
            except Exception:
                pass
    st.info("未找到 exchange 的账户查询方法（get_account/fetch_account/account/get_balances）。")


def render_account_page() -> None:
    exchange = st.session_state.get("exchange")
    if exchange is None:
        st.info("请先在左侧点击「初始化 / 重新连接」")
        return
    symbol_filter = st.text_input("symbol 过滤（可空）", value=st.session_state.get("selected_symbol", "")).strip().upper().replace("/", "")
    symbol_filter = symbol_filter or None
    render_account_panel(exchange, symbol=symbol_filter)


def main() -> None:
    st.set_page_config(page_title="Trading Control Panel", layout="wide")
    st.title("📟 Trading Control Panel（扫描 + 阶梯 + Short Trailing + Short Trailing Stack + 手动下单）")

    ui_logger = _ensure_ui_logger_registered()
    page = render_sidebar()

    if page == "hammer":
        render_hammer_scanner()
    elif page == "ladder":
        render_ladder_and_manual()
    elif page == "short_trailing":
        render_short_trailing()
    elif page == "short_trailing_stack":
        render_short_trailing_stack()
    elif page == "logs":
        render_logs_page(ui_logger)
    elif page == "account":
        render_account_page()
    else:
        st.info("未知页面")

    # ==============================
    # Positions global poller
    # ==============================
    exchange = st.session_state.get("exchange")
    if exchange is not None:
        now = time.time()
        last_poll = st.session_state.get("positions_last_poll_ts", 0)

        # 每 15 秒最多刷新一次全量持仓
        if now - last_poll > 15:
            try:
                exchange.get_positions(None)  # ✅ 全项目唯一的全量调用点
                st.session_state["positions_last_poll_ts"] = now
            except Exception as e:
                logger.warning(f"positions poller failed: {e}")


if __name__ == "__main__":
    main()
