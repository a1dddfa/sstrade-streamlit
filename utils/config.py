import os

def load_config(cfg: dict):
    # -------- safe access (关键修复点) --------
    binance_cfg = cfg.get("binance", {}) or {}
    global_cfg = cfg.get("global", {}) or {}

    # -------- Binance credentials (env 优先) --------
    api_key = os.getenv(
        "BINANCE_API_KEY",
        binance_cfg.get("api_key", "")
    )

    api_secret = os.getenv(
        "BINANCE_API_SECRET",
        binance_cfg.get("api_secret", "")
    )
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()

    # -------- Proxy configuration (global fallback) --------
    global_proxy = os.getenv(
        "GLOBAL_PROXY",
        global_cfg.get("proxy")
    )

    proxy = os.getenv(
        "BINANCE_PROXY",
        binance_cfg.get("proxy") or global_proxy
    )

    # -------- WebSocket switch --------
    use_ws_env = os.getenv("USE_WS")
    if use_ws_env is not None:
        use_ws = use_ws_env.lower() in ("1", "true", "yes", "on")
    else:
        use_ws = global_cfg.get("use_ws", False)

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "proxy": proxy,
        "use_ws": use_ws,
    }
