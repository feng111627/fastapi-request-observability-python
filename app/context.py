"""Request-scoped trace context built on contextvars."""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class StageTiming:
    """Timing of one named stage within a request."""

    name: str
    duration_ms: float


@dataclass
class TraceContext:
    """Per-request trace state propagated across sync/async/background work."""

    trace_id: str
    path: str = ""
    method: str = ""
    error_kind: str = "none"
    stages: list[StageTiming] = field(default_factory=list)
    # Pre-decided at request start: True means this request's segments go
    # to the replay buffer instead of the main log, unless the request
    # turns out to be an error or slow (those are always kept in the
    # main log). Lives in the context so background tasks inheriting the
    # context route their segments the same way, and concurrent requests
    # never see each other's flag.
    sampled_out: bool = False

    def add_stage(self, name: str, duration_ms: float) -> None:
        self.stages.append(StageTiming(name=name, duration_ms=round(duration_ms, 3)))

    def mark_error(self, kind: str) -> None:
        # first classification wins; uncaught may override handled kinds
        if self.error_kind == "none" or kind == "uncaught":
            self.error_kind = kind


_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "current_trace", default=None
)


def get_trace() -> TraceContext | None:
    return _current_trace.get()


def set_trace(ctx: TraceContext) -> contextvars.Token:
    return _current_trace.set(ctx)


def reset_trace(token: contextvars.Token) -> None:
    _current_trace.reset(token)


def current_trace_id() -> str:
    ctx = _current_trace.get()
    return ctx.trace_id if ctx is not None else "-"


def mark_error(kind: str) -> None:
    ctx = _current_trace.get()
    if ctx is not None:
        ctx.mark_error(kind)


@contextmanager
def timed_stage(name: str) -> Iterator[None]:
    """Record a named stage's duration into the current trace (sync or async)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        ctx = _current_trace.get()
        if ctx is not None:
            ctx.add_stage(name, (time.perf_counter() - start) * 1000)
