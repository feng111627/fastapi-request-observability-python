"""Observed background tasks.

A task scheduled via :func:`run_in_background`

* inherits its spawning request's trace context (contextvars copy),
* is held by a strong reference set until it finishes, so returning the
  response can never drop it,
* is supervised: success or failure is classified, counted in metrics,
  stored by trace id, and emitted as its own log segment, so a failure
  can never be swallowed silently and every result is attributable to
  its request.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.errors import ErrorKind
from app.events import emit_event
from app.metrics import metrics_registry

MAX_RESULT_CHARS = 2000


def _truncate(value: object, limit: int = MAX_RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


@dataclass
class TaskResult:
    """Outcome of one background task, attributable to its request."""

    trace_id: str
    name: str
    status: str  # "success" | "failed"
    started_at_ms: float
    finished_at_ms: float
    duration_ms: float
    error_kind: str = "none"
    error: str = ""
    result: str = ""

    def as_event(self) -> dict:
        d = asdict(self)
        return d


def _classify(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    if kind is not None:
        return str(kind)
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ErrorKind.TIMEOUT.value
    return ErrorKind.UNCAUGHT.value


class TaskResultStore:
    """Bounded in-memory store of task results keyed by trace id.

    Global cap (oldest evicted first) and a per-trace cap protect memory
    when one request schedules very many tasks.
    """

    def __init__(self, max_results: int = 2000, per_trace_cap: int = 100) -> None:
        self._max_results = max_results
        self._per_trace_cap = per_trace_cap
        self._by_trace: dict[str, list[TaskResult]] = defaultdict(list)
        self._order: OrderedDict[str, None] = OrderedDict()

    def add(self, result: TaskResult) -> None:
        bucket = self._by_trace[result.trace_id]
        bucket.append(result)
        if len(bucket) > self._per_trace_cap:
            del bucket[: len(bucket) - self._per_trace_cap]
        self._order[result.trace_id] = None
        self._enforce_global_cap()

    def _enforce_global_cap(self) -> None:
        total = sum(len(v) for v in self._by_trace.values())
        while total > self._max_results and self._order:
            oldest_id, _ = self._order.popitem(last=False)
            total -= len(self._by_trace.pop(oldest_id, ()))

    def get(self, trace_id: str) -> list[TaskResult]:
        return list(self._by_trace.get(trace_id, ()))

    def all_results(self) -> list[TaskResult]:
        out: list[TaskResult] = []
        for bucket in self._by_trace.values():
            out.extend(bucket)
        return out

    def reset(self) -> None:
        self._by_trace.clear()
        self._order.clear()


task_result_store = TaskResultStore()

# Strong references until tasks complete: asyncio only keeps weak refs.
_background_tasks: set[asyncio.Task] = set()


async def _supervise(
    coro_factory: Callable[[], Awaitable[Any]], trace_id: str, name: str
) -> None:
    started = time.time()
    start_pc = time.perf_counter()
    result = TaskResult(
        trace_id=trace_id,
        name=name,
        status="failed",
        started_at_ms=round(started * 1000, 3),
        finished_at_ms=0.0,
        duration_ms=0.0,
    )
    try:
        value = await coro_factory()
        result.status = "success"
        result.result = _truncate(value)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - supervisor must not swallow silently
        result.error_kind = _classify(exc)
        result.error = _truncate(f"{type(exc).__name__}: {exc}")
    finally:
        result.finished_at_ms = round(time.time() * 1000, 3)
        result.duration_ms = round((time.perf_counter() - start_pc) * 1000, 3)
        task_result_store.add(result)
        metrics_registry.record_background(error_kind=result.error_kind)
        emit_event("background_task", segment="background_task", **result.as_event())


def run_in_background(
    coro_factory: Callable[[], Awaitable[Any]], name: str | None = None
) -> asyncio.Task:
    """Schedule a traced, supervised background task.

    Runs inside a copy of the current context, so the task keeps the
    spawning request's trace id even after the response is sent.
    """
    trace_id = contextvars.copy_context().run(lambda: _ambient_trace_id())
    task_name = name or getattr(coro_factory, "__name__", "background-task")
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    task = loop.create_task(ctx.run(_supervise, coro_factory, trace_id, task_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _ambient_trace_id() -> str:
    from app.context import current_trace_id

    return current_trace_id()
