# -*- coding: utf-8 -*-
"""统一日志配置

- 控制台 + logs/framework.log（滚动）
- 可选 logs/trade.log（只记录交易关键事件）

使用方式（main.py 顶部尽早调用）：
    from core.logging_config import setup_logging
    setup_logging(log_dir="logs", level=logging.INFO)
"""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> None:
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)

    # 清空旧 handler，避免重复输出
    root.handlers.clear()

    # ===== 控制台 =====
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # ===== 主日志文件（滚动）=====
    fh = RotatingFileHandler(
        os.path.join(log_dir, "framework.log"),
        maxBytes=20 * 1024 * 1024,   # 20MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # ===== 交易日志（单独文件）=====
    trade_logger = logging.getLogger("trade")
    trade_logger.setLevel(logging.INFO)

    trade_fh = RotatingFileHandler(
        os.path.join(log_dir, "trade.log"),
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    trade_fh.setLevel(logging.INFO)
    trade_fh.setFormatter(fmt)

    # 避免重复添加
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "").endswith("trade.log")
               for h in trade_logger.handlers):
        trade_logger.addHandler(trade_fh)

    # 是否让 trade 也同步进 framework.log（想完全分离就设 False）
    trade_logger.propagate = True
