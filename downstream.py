"""Local downstream service used to demo cross-service trace propagation.

Runs standalone (``python downstream.py``, port 8001) or in-process via
``run_server()`` (used by tests and by ``python main.py --with-downstream``).
It keeps its own settings holder and log file, but shares the process-wide
segment store so a same-process deployment can replay the full chain.
"""
from __future__ import annotations

import asyncio
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import ObservabilitySettings, SettingsHolder
from app.context import current_trace_id, timed_stage
from app.errors import BusinessError, ErrorKind
from app.context import mark_error
from app.middleware import TraceMiddleware

downstream_settings = SettingsHolder(ObservabilitySettings(
    service_name="downstream",
    log_file="downstream.log",
))

app = FastAPI(title="observability-demo-downstream")
app.add_middleware(TraceMiddleware, settings=downstream_settings)


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    mark_error(ErrorKind.BUSINESS.value)
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "code": exc.code, "trace_id": current_trace_id()},
    )


@app.get("/work")
async def work(ms: int = 20, fail: bool = False) -> dict:
    with timed_stage("downstream-work"):
        await asyncio.sleep(ms / 1000)
    if fail:
        raise BusinessError("downstream rejected the job", code="DOWNSTREAM_REJECT")
    return {"ok": True, "service": "downstream", "trace_id": current_trace_id()}


def run_server(port: int = 8001) -> uvicorn.Server:
    """Start the downstream in a daemon thread; returns the running server."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
