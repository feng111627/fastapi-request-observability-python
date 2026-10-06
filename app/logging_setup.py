"""JSON logging with concurrency-safe rotation."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "trace", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler serialising emit+rotation with a lock.

    Prevents interleaved writes and lost content when many threads/tasks
    log concurrently through the same handler.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._emit_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._emit_lock:
            super().emit(record)


_loggers: dict[str, logging.Logger] = {}
_loggers_lock = threading.Lock()


def replay_log_file(log_file: str) -> str:
    """Name of the replay-buffer log derived from the main log file name.

    Sampled-out requests' segments go here instead of the main log, so
    the main log keeps only kept requests while every trace that really
    happened stays replayable by id. ``requests.log`` maps to
    ``requests.replay.log``, which the main log's ``requests.log*`` glob
    does not match.
    """
    root, ext = os.path.splitext(log_file)
    return f"{root}.replay{ext or '.log'}"


def get_request_logger(log_dir: str, log_file: str, max_bytes: int, backup_count: int) -> logging.Logger:
    """Return (creating once) a JSON logger writing to a rotating file."""
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, log_file)
    with _loggers_lock:
        if path in _loggers:
            return _loggers[path]
        logger = logging.getLogger(f"observability.{path}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = SafeRotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        _loggers[path] = logger
        return logger


def reset_loggers() -> None:
    """Close and forget cached loggers (tests / settings-driven rebuild).

    Rotation parameters are fixed when a handler is created; changing
    them at runtime requires recreating the handler via this reset.
    """
    with _loggers_lock:
        for logger in _loggers.values():
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        _loggers.clear()
