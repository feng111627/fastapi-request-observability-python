"""ASGI middleware: trace id, staging, classification, sampling, metrics."""
from __future__ import annotations

import json
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import SettingsHolder, settings_holder
from app.context import TraceContext, reset_trace, set_trace
from app.errors import ErrorKind
from app.logging_setup import get_request_logger
from app.metrics import metrics_registry
from app.propagate import TRACE_HEADER
from app.sampling import sampling_policy
from app.segments import Segment, segment_store

_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _extract_trace_id(scope: Scope) -> str:
    """Accept a well-formed client trace id, otherwise generate a fresh one.

    Client-supplied ids are validated and only ever stored in this
    request's own context, so they cannot leak into other requests.
    """
    for name, value in scope.get("headers", []):
        if name == TRACE_HEADER.lower().encode():
            candidate = value.decode("latin-1").strip()
            if _TRACE_ID_RE.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class TraceMiddleware:
    """Pure-ASGI middleware so it wraps routing and exception handlers.

    ``settings`` defaults to the process-global holder; a co-located
    second service (e.g. the demo downstream) can pass its own holder
    so the two services keep independent thresholds/log files.
    """

    def __init__(self, app: ASGIApp, settings: SettingsHolder | None = None) -> None:
        self.app = app
        self._settings = settings or settings_holder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = self._settings.get()
        ctx = TraceContext(
            trace_id=_extract_trace_id(scope),
            path=scope.get("path", ""),
            method=scope.get("method", ""),
        )
        token = set_trace(ctx)
        status_code = 500
        start = time.perf_counter()
        started_at = time.time()
        try:
            await self.app(scope, receive, self._wrap_send(send, ctx, scope))
            # status captured by wrapped send; default below if never set
            status_code = getattr(ctx, "_status_code", 200)  # noqa: B009
        except Exception as exc:  # uncaught: classify, log, synthesise 500
            ctx.mark_error(ErrorKind.UNCAUGHT.value)
            status_code = 500
            await self._send_500(send, ctx, exc)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self._finalize(ctx, status_code, duration_ms, settings, started_at)
            reset_trace(token)

    def _wrap_send(self, send: Send, ctx: TraceContext, scope: Scope) -> Send:
        async def sender(message: Message) -> None:
            if message["type"] == "http.response.start":
                ctx._status_code = message["status"]  # type: ignore[attr-defined]
                headers = list(message.get("headers", []))
                headers.append((TRACE_HEADER.lower().encode(), ctx.trace_id.encode()))
                message["headers"] = headers
            await send(message)

        return sender

    async def _send_500(self, send: Send, ctx: TraceContext, exc: Exception) -> None:
        body = json.dumps(
            {"detail": "internal server error", "trace_id": ctx.trace_id,
             "error_kind": ErrorKind.UNCAUGHT.value}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (TRACE_HEADER.lower().encode(), ctx.trace_id.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    def _finalize(
        self, ctx: TraceContext, status_code: int, duration_ms: float, settings,
        started_at: float,
    ) -> None:
        is_slow = duration_ms >= settings.slow_request_ms
        is_error = ctx.error_kind != ErrorKind.NONE.value
        keep = sampling_policy.should_keep(is_error=is_error, is_slow=is_slow)
        metrics_registry.record(error_kind=ctx.error_kind, is_slow=is_slow, logged=keep)
        # Segments are recorded regardless of sampling so replay stays possible.
        segment_store.add(Segment(
            kind="request",
            service=settings.service_name,
            name=f"{ctx.method} {ctx.path}",
            trace_id=ctx.trace_id,
            started_at=started_at,
            duration_ms=duration_ms,
            error_kind=ctx.error_kind,
            detail={
                "status": status_code,
                "slow": is_slow,
                "stages": [{"name": s.name, "duration_ms": s.duration_ms} for s in ctx.stages],
            },
        ))
        if not keep:
            return
        logger = get_request_logger(
            settings.log_dir, settings.log_file,
            settings.rotation_max_bytes, settings.rotation_backup_count,
        )
        logger.info(
            "request",
            extra={"trace": {
                "trace_id": ctx.trace_id,
                "method": ctx.method,
                "path": ctx.path,
                "status": status_code,
                "duration_ms": round(duration_ms, 3),
                "slow": is_slow,
                "slow_threshold_ms": settings.slow_request_ms,
                "error_kind": ctx.error_kind,
                "stages": [{"name": s.name, "duration_ms": s.duration_ms} for s in ctx.stages],
            }},
        )
