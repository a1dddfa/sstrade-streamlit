import os

def load_config(cfg: dict):
    # ---- Binance credentials ----
    api_key = os.getenv(
        "BINANCE_API_KEY",
        cfg["binance"].get("api_key", "")
    )

    api_secret = os.getenv(
        "BINANCE_API_SECRET",
        cfg["binance"].get("api_secret", "")
    )

    proxy = os.getenv(
        "BINANCE_PROXY",
        cfg["binance"].get("proxy")
    )

    # ---- WebSocket switch ----
    use_ws_env = os.getenv("USE_WS")
    if use_ws_env is not None:
        use_ws = use_ws_env.lower() in ("1", "true", "yes", "on")
    else:
        use_ws = cfg["global"].get("use_ws", False)

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "proxy": proxy,
        "use_ws": use_ws,
    }
