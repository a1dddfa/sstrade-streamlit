# -*- coding: utf-8 -*-
"""
Exchange (re)initialization service for Streamlit Trading Control Panel.

Step-2 refactor target:
- Move the "init/reconnect exchange" side-effect logic out of UI code.
- Keep runtime behavior identical to the original streamlit_app.py.

Current design (transitional):
- We dynamically resolve legacy functions from the Streamlit main module (__main__):
    - load_config(cfg_path) -> dict
    - init_exchange(cfg, override_dry_run=...) -> exchange instance
    - _get_user_stream_dispatcher() -> dispatcher with handle_order_update/register_* methods
    - logger (python logging.Logger) to log exceptions
- This allows incremental refactor without breaking imports.

Next steps (recommended):
- Replace _resolve_from_main with explicit imports from your project modules once the
  functions are moved into services/config modules.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import streamlit as st


def _resolve_from_main(name: str):
    """Resolve a symbol from the Streamlit main script module (__main__)."""
    import sys

    main = sys.modules.get("__main__")
    if main is None or not hasattr(main, name):
        raise RuntimeError(
            f"exchange_service.py expected `{name}` to exist in the Streamlit main script. "
            f"Please keep `{name}` in streamlit_app.py for now, or pass explicit callbacks."
        )
    return getattr(main, name)


def cleanup_old_exchange() -> None:
    """Disconnect old exchange + user stream, if any, to avoid WS residue."""
    old_ex = st.session_state.get("exchange")
    if old_ex is None:
        return

    try:
        old_ex.ws_unsubscribe_user_stream()
    except Exception:
        pass
    try:
        old_ex.ws_disconnect()
    except Exception:
        pass


def rebind_bots_to_exchange(new_ex: Any) -> None:
    """Point LadderBot/RangeTwoBot (if exist in session) to the new exchange."""
    lb = st.session_state.get("ladder_bot")
    if lb is not None:
        try:
            lb.stop()
        except Exception:
            pass
        lb.exchange = new_ex
        # Some bots keep a subscribed symbol; clear it so the bot can resubscribe cleanly.
        if hasattr(lb, "_sub_symbol"):
            lb._sub_symbol = None

    rb = st.session_state.get("range2_bot")
    if rb is not None:
        try:
            rb.stop()
        except Exception:
            pass
        rb.exchange = new_ex
        if hasattr(rb, "_sub_symbol"):
            rb._sub_symbol = None


def register_bots_to_user_stream_dispatcher() -> None:
    """Register bots to the session-scoped dispatcher (range2 bot is used for WS callbacks)."""
    _get_user_stream_dispatcher = _resolve_from_main("_get_user_stream_dispatcher")
    dispatcher = _get_user_stream_dispatcher()
    rb = st.session_state.get("range2_bot")
    # rb could be None; registering None is used to clear old reference in your dispatcher.
    try:
        dispatcher.register_range2_bot(rb)
    except Exception:
        # If dispatcher signature differs, don't hard fail during refactor step.
        pass


def subscribe_user_stream_once(new_ex: Any) -> bool:
    """
    Subscribe user stream only once per session.

    Returns True if a subscription happened, False if it was already subscribed.
    """
    if st.session_state.get("_user_stream_subscribed"):
        return False

    _get_user_stream_dispatcher = _resolve_from_main("_get_user_stream_dispatcher")
    dispatcher = _get_user_stream_dispatcher()

    new_ex.ws_subscribe_user_stream(dispatcher.handle_order_update)
    st.session_state["_user_stream_subscribed"] = True
    return True


def init_exchange_flow(
    cfg_path: str,
    *,
    override_dry_run: bool = False,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """
    End-to-end (re)initialization used by the UI button.

    Side effects:
    - reads config
    - cleans up old exchange WS
    - creates new exchange
    - stores into st.session_state["exchange"]
    - rebinds bots
    - registers bots to dispatcher
    - subscribes user stream once
    - writes Streamlit success/info/error messages

    Returns:
        (exchange, cfg_dict) where cfg_dict is the loaded config (or None on failure).
    """
    load_config = _resolve_from_main("load_config_file")
    init_exchange = _resolve_from_main("init_exchange")
    logger = _resolve_from_main("logger")

    try:
        cfg: Dict[str, Any] = load_config(cfg_path)
        # Keep parity with original code (global_cfg used to exist)
        _ = cfg.get("global") or {}
        st.session_state["app_cfg"] = cfg

        cleanup_old_exchange()

        # Reset subscribe flag so new exchange can subscribe again
        st.session_state["_user_stream_subscribed"] = False

        new_ex = init_exchange(cfg, override_dry_run=override_dry_run)
        st.session_state["exchange"] = new_ex

        rebind_bots_to_exchange(new_ex)
        register_bots_to_user_stream_dispatcher()

        st.success("交易所已初始化 / 已重连（已清理旧 WS）")

        if subscribe_user_stream_once(new_ex):
            st.info("✅ 已订阅用户数据流（订单 / 账户更新）")

        return new_ex, cfg

    except Exception as e:
        st.error(f"初始化/重连失败：{e}")
        try:
            logger.exception("init_exchange failed")
        except Exception:
            pass
        return None, None
