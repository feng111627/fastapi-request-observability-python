"""JSON 结构化日志与并发安全的轮转配置。

说明：logging 的 Handler 自带线程锁，同一进程内的并发写入不会交错；
RotatingFileHandler 的轮转也在同一把锁内完成。多进程写同一文件不在
保障范围内（见 README 已知限制）。
"""
from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "observability.request"


class JsonFormatter(logging.Formatter):
    """把 record 的 message（已是 dict）序列化为单行 JSON，保证不交错。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()}
        payload.setdefault("level", record.levelname)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_file: str, max_bytes: int, backup_count: int) -> logging.Logger:
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 重复调用时先清掉旧 handler，避免测试/重载场景下重复写
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
