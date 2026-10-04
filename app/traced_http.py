"""HTTP client calls that propagate and verify the trace id.

The caller's ``X-Trace-Id`` is attached to the request; the downstream
service is expected to echo it. The echo is compared against the id we
sent and any mismatch is surfaced loudly (``TraceMismatchError`` +
metric + log segment), so crossed identifiers cannot pass silently.

For the local, offline reproduction the call can travel over an
in-process ASGI transport. It runs the downstream app in a *fresh*
context (``IsolatedASGITransport``), so this proves real cross-context
propagation rather than the id leaking through a shared contextvar.
"""
from __future__ import annotations

import asyncio
import contextvars
import time

import httpx

from app.context import current_trace_id
from app.errors import ErrorKind, TracePropagationError
from app.events import emit_event
from app.metrics import metrics_registry
from app.propagate import TRACE_HEADER

LOCAL_ASGI_BASE = "http://downstream.local"


class IsolatedASGITransport(httpx.ASGITransport):
    """Run the ASGI app in an empty context, like a separate service.

    Without this the in-process call would execute with the caller's
    contextvars still set, and the "propagation" would be accidental
    context leakage. A fresh ``Context`` makes header propagation the
    only way the id can cross.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await contextvars.Context().run(super().handle_async_request, request)


from dataclasses import dataclass  # noqa: E402


@dataclass
class DownstreamCall:
    """Result of one verified downstream call."""

    url: str
    status_code: int
    trace_sent: str
    trace_received: str
    matched: bool
    duration_ms: float
    body: object


@dataclass
class _Clients:
    real: httpx.AsyncClient | None = None
    asgi: httpx.AsyncClient | None = None


# One client pair *per event loop*. A single AsyncClient binds to the
# loop that first uses it; sharing it across loops (several test
# portals, or nested loops) fails. Lazily creating per loop is safe and
# mirrors one client per running process loop in production.
_config: dict[str, object] = {}
_loop_clients: dict[asyncio.AbstractEventLoop, _Clients] = {}


def _prune_closed_loops() -> None:
    for loop in [loop for loop in _loop_clients if loop.is_closed()]:
        _loop_clients.pop(loop, None)


def configure_clients(
    asgi_app: object | None = None, *, real_base_url: str | None = None
) -> None:
    """Configure outbound clients.

    ``asgi_app`` gives an in-process downstream (tests, offline demo);
    ``real_base_url`` gives a real HTTP base URL (two local processes).
    The actual :class:`httpx.AsyncClient` instances are created lazily
    per running event loop on first use.
    """
    _config["asgi_app"] = asgi_app
    _config["real_base_url"] = real_base_url
    _prune_closed_loops()


def _get_clients() -> _Clients:
    loop = asyncio.get_running_loop()
    clients = _loop_clients.get(loop)
    if clients is not None:
        return clients
    asgi_app = _config.get("asgi_app")
    real_base_url = _config.get("real_base_url")
    clients = _Clients(
        real=httpx.AsyncClient(base_url=real_base_url) if real_base_url else None,
        asgi=httpx.AsyncClient(
            transport=IsolatedASGITransport(app=asgi_app), base_url=LOCAL_ASGI_BASE
        )
        if asgi_app is not None
        else None,
    )
    _loop_clients[loop] = clients
    return clients


async def aclose_http() -> None:
    """Close clients owned by the running event loop (test teardown)."""
    loop = asyncio.get_running_loop()
    clients = _loop_clients.pop(loop, None)
    if clients is None:
        return
    for client in (clients.real, clients.asgi):
        if client is not None:
            await client.aclose()


async def call_downstream(
    path: str,
    *,
    method: str = "GET",
    lie: bool = False,
    timeout: float = 5.0,
    headers: dict[str, str] | None = None,
    use_real: bool = False,
) -> DownstreamCall:
    """Call a downstream URL propagating X-Trace-Id and verify the echo.

    By default ``path`` is sent to the in-process downstream ASGI app;
    ``use_real=True`` sends it to the configured real HTTP base URL.
    ``lie=True`` asks the demo downstream to echo a wrong id on purpose
    (negative test).
    """
    trace_sent = current_trace_id()
    url = path
    clients = _get_clients()
    client = clients.real if use_real else clients.asgi
    if client is None:
        raise RuntimeError("no http client configured; call configure_clients(...)")

    sent_headers = dict(headers or {})
    sent_headers[TRACE_HEADER] = trace_sent
    params = {"lie": "1"} if lie else None
    start = time.perf_counter()
    response = await client.request(
        method, url, headers=sent_headers, params=params, timeout=timeout
    )
    duration_ms = round((time.perf_counter() - start) * 1000, 3)

    body: object
    try:
        body = response.json()
    except ValueError:
        body = response.text
    trace_received = "-"
    if isinstance(body, dict):
        trace_received = str(body.get("trace_id", "-"))
    matched = trace_received == trace_sent

    segment = DownstreamCall(
        url=url,
        status_code=response.status_code,
        trace_sent=trace_sent,
        trace_received=trace_received,
        matched=matched,
        duration_ms=duration_ms,
        body=body,
    )
    emit_event(
        "downstream_call",
        segment="downstream_call",
        url=url,
        method=method,
        downstream_status=response.status_code,
        duration_ms=duration_ms,
        trace_sent=trace_sent,
        trace_received=trace_received,
        matched=matched,
    )
    if not matched:
        metrics_registry.record_mismatch()
        raise TracePropagationError(
            f"downstream {url} echoed trace {trace_received!r}, sent {trace_sent!r}"
        )
    return segment
