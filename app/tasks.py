"""Observable background tasks: traced, tracked, classified, replayable."""
from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings_holder
from app.context import current_trace_id
from app.errors import ErrorKind
from app.metrics import metrics_registry
from app.segments import Segment, segment_store

# Strong references keep tasks alive after the request that scheduled
# them has finished; without this the loop may GC a pending task.
_pending: set[asyncio.Task] = set()

_RESULT_MAX_LEN = 200


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _RESULT_MAX_LEN else text[:_RESULT_MAX_LEN] + "..."


def run_observed(
    name: str, coro_factory: Callable[[], Awaitable[Any]]
) -> asyncio.Task:
    """Schedule a coroutine inheriting the current trace context.

    On completion a ``background`` segment is recorded under the same
    trace id (with a short result summary); on failure the segment is
    classified ``background`` and the failure is counted in metrics, so
    a failed task is never silently swallowed.
    """
    ctx = contextvars.copy_context()
    trace_id = current_trace_id()
    service = settings_holder.get().service_name
    started_at = time.time()
    start = time.perf_counter()

    async def runner() -> None:
        error_kind = ErrorKind.NONE.value
        detail: dict[str, Any] = {}
        try:
            result = await coro_factory()
            detail["result"] = _short(result)
        except Exception as exc:  # classified + counted, never swallowed
            error_kind = ErrorKind.BACKGROUND.value
            detail["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            segment_store.add(Segment(
                kind="background",
                service=service,
                name=name,
                trace_id=trace_id,
                started_at=started_at,
                duration_ms=(time.perf_counter() - start) * 1000,
                error_kind=error_kind,
                detail=detail,
            ))
            metrics_registry.record_background(failed=error_kind != ErrorKind.NONE.value)

    task = asyncio.get_running_loop().create_task(runner(), context=ctx)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
