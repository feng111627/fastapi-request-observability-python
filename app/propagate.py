"""Helpers to propagate trace context to background tasks and outbound calls."""
from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from typing import Any

from app.context import current_trace_id

TRACE_HEADER = "X-Trace-Id"


def trace_headers() -> dict[str, str]:
    """Headers to attach to outbound HTTP calls so downstream sees the same id."""
    return {TRACE_HEADER: current_trace_id()}


def run_in_background(coro_factory: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """Schedule a coroutine that inherits the current trace context.

    The factory is invoked inside a copied context, so the background
    task sees the caller's trace id while remaining isolated from later
    requests handled by the same worker.
    """
    ctx = contextvars.copy_context()
    return asyncio.get_running_loop().create_task(ctx.run(_wrap, coro_factory))


def _wrap(coro_factory: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
    return coro_factory()
