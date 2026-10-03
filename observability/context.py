"""基于 contextvars 的链路上下文：保证并发请求之间互不串号。"""
from __future__ import annotations

import contextvars
import re
import uuid
from typing import Callable, TypeVar

TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observability_trace_id", default=None
)

F = TypeVar("F", bound=Callable)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(trace_id: str) -> contextvars.Token:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


def sanitize_incoming(raw: str | None) -> str | None:
    """校验客户端传入的标识；不合法则返回 None（由服务端重新生成）。"""
    if raw and TRACE_ID_PATTERN.match(raw):
        return raw
    return None


def bind_to_current_context(func: F) -> F:
    """捕获当前上下文，让后台任务/线程池回调沿用同一 trace_id。"""
    ctx = contextvars.copy_context()

    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ctx.run(func, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
