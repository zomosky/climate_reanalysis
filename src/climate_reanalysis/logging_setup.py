"""structlog 配置（与兄弟项目一致的 KV 输出）。"""

from __future__ import annotations

import logging

import structlog


def configure(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ]
    )


def get_logger(name: str):
    return structlog.get_logger(name)
