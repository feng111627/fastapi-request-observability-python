"""Local downstream service for reproducing cross-service tracing.

Run it as its own process (``python -m app.downstream`` on port 8001)
or mount it in-process via ASGI transport in tests. It does its own
work, logs under the trace id it received from the upstream, and echoes
that id so the upstream can verify propagation end to end.
"""
from __future__ import annotations

import asyncio
import uuid

import uvicorn
from fastapi import FastAPI, Request

from app.context import current_trace_id, timed_stage
from app.middleware import TraceMiddleware
from app.propagate import TRACE_HEADER

downstream_app = FastAPI(title="observability-downstream")
downstream_app.add_middleware(TraceMiddleware, service_name="downstream")


@downstream_app.get("/echo")
async def echo(request: Request, lie: bool = False, work_ms: int = 5) -> dict:
    """Process work under the incoming trace id and echo it.

    ``lie=True`` returns a different id in the body (but keeps the real
    id in the response header), simulating a downstream that fails to
    propagate correctly so the upstream's verification can be tested.
    """
    with timed_stage("downstream-work"):
        await asyncio.sleep(work_ms / 1000)
    received = request.headers.get(TRACE_HEADER, "-")
    echoed = uuid.uuid4().hex if lie else received
    return {
        "service": "downstream",
        "received_trace_id": received,
        "trace_id": echoed,
        "header_trace_id": current_trace_id(),
    }


@downstream_app.get("/health")
async def health() -> dict:
    return {"ok": True, "trace_id": current_trace_id()}


if __name__ == "__main__":
    uvicorn.run(downstream_app, host="127.0.0.1", port=8001)
