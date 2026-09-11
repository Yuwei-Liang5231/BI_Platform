"""日志配置：统一格式、统一入口，全应用共用。"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志。重复调用安全（不叠加 handler）。"""
    root = logging.getLogger()
    if getattr(root, "_bi_configured", False):
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())
    root._bi_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
