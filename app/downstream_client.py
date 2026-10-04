"""Outbound call helper: propagate trace id, verify the echo, record a segment."""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import settings_holder
from app.context import current_trace_id
from app.errors import DownstreamError, ErrorKind, TraceMismatchError
from app.propagate import TRACE_HEADER, trace_headers
from app.segments import Segment, segment_store


async def call_downstream(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET the configured downstream with the current trace id attached.

    The downstream echoes the id it saw in the ``X-Trace-Id`` response
    header; a mismatch (or a failed call) raises a classified error and
    is recorded as a ``downstream`` segment either way, so the failure
    is visible in metrics, logs and replay.
    """
    settings = settings_holder.get()
    url = settings.downstream_url.rstrip("/") + path
    sent_id = current_trace_id()
    started_at = time.time()
    start = time.perf_counter()
    status: int | None = None
    echoed: str | None = None
    error_kind = ErrorKind.NONE.value
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=trace_headers(), params=params)
        status = resp.status_code
        echoed = resp.headers.get(TRACE_HEADER.lower()) or resp.headers.get(TRACE_HEADER)
        if echoed != sent_id:
            error_kind = ErrorKind.TRACE_MISMATCH.value
            raise TraceMismatchError(sent_id, echoed)
        if status >= 500:
            error_kind = ErrorKind.DOWNSTREAM.value
            raise DownstreamError(f"downstream returned {status}")
        return resp.json()
    except (TraceMismatchError, DownstreamError):
        raise
    except httpx.HTTPError as exc:
        error_kind = ErrorKind.DOWNSTREAM.value
        raise DownstreamError(f"downstream call failed: {exc}") from exc
    finally:
        segment_store.add(Segment(
            kind="downstream",
            service=settings.service_name,
            name=f"GET {path}",
            trace_id=sent_id,
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
            error_kind=error_kind,
            detail={"url": url, "status": status, "sent_trace_id": sent_id,
                    "echoed_trace_id": echoed},
        ))
