"""Demo service wiring the observability stack together."""
from __future__ import annotations

import asyncio
import random

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings_holder
from app.context import current_trace_id, timed_stage
from app.downstream_client import call_downstream
from app.errors import (
    BusinessError,
    DownstreamError,
    ErrorKind,
    OperationTimeout,
    TraceMismatchError,
)
from app.context import mark_error
from app.metrics import metrics_registry
from app.middleware import TraceMiddleware
from app.propagate import run_in_background, trace_headers
from app.replay import build_timeline
from app.sampling import sampling_policy
from app.segments import segment_store
from app.tasks import run_observed

app = FastAPI(title="observability-demo")
app.add_middleware(TraceMiddleware)


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    mark_error(ErrorKind.BUSINESS.value)
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "code": exc.code, "trace_id": current_trace_id()},
    )


@app.exception_handler(OperationTimeout)
async def timeout_handler(request: Request, exc: OperationTimeout) -> JSONResponse:
    mark_error(ErrorKind.TIMEOUT.value)
    return JSONResponse(
        status_code=504,
        content={"detail": str(exc), "trace_id": current_trace_id()},
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    mark_error(ErrorKind.VALIDATION.value)
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "trace_id": current_trace_id()},
    )


@app.exception_handler(DownstreamError)
async def downstream_error_handler(request: Request, exc: DownstreamError) -> JSONResponse:
    mark_error(ErrorKind.DOWNSTREAM.value)
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "trace_id": current_trace_id(),
                 "error_kind": ErrorKind.DOWNSTREAM.value},
    )


@app.exception_handler(TraceMismatchError)
async def trace_mismatch_handler(request: Request, exc: TraceMismatchError) -> JSONResponse:
    mark_error(ErrorKind.TRACE_MISMATCH.value)
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "trace_id": current_trace_id(),
                 "error_kind": ErrorKind.TRACE_MISMATCH.value,
                 "sent_trace_id": exc.sent, "echoed_trace_id": exc.echoed},
    )


@app.get("/")
def read_root() -> dict:
    return {"Hello": "World", "trace_id": current_trace_id()}


class OrderIn(BaseModel):
    item: str
    quantity: int


@app.post("/orders")
async def create_order(order: OrderIn) -> dict:
    with timed_stage("validate-stock"):
        await asyncio.sleep(0.01)
    if order.quantity > 10:
        raise BusinessError("quantity exceeds limit", code="QUOTA_EXCEEDED")
    with timed_stage("persist"):
        await asyncio.sleep(0.01)
    return {"ok": True, "trace_id": current_trace_id()}


@app.get("/slow")
async def slow(ms: int = 600) -> dict:
    with timed_stage("heavy-compute"):
        await asyncio.sleep(ms / 1000)
    return {"ms": ms, "trace_id": current_trace_id()}


@app.get("/timeout")
async def timeout() -> dict:
    await asyncio.sleep(0.05)
    raise OperationTimeout("downstream did not answer in time")


@app.get("/crash")
async def crash() -> dict:
    raise RuntimeError("boom")


@app.get("/background")
async def background() -> dict:
    async def work() -> None:
        with timed_stage("background-work"):
            await asyncio.sleep(0.01)

    run_in_background(work)
    return {"scheduled": True, "trace_id": current_trace_id()}


@app.get("/outbound")
async def outbound() -> dict:
    # In a real service these headers go onto the httpx/requests call.
    return {"would_send_headers": trace_headers()}


@app.get("/relay")
async def relay(ms: int = 20, fail: bool = False) -> dict:
    """Call the downstream service for real and verify the echoed trace id."""
    sent = current_trace_id()
    with timed_stage("downstream-call"):
        result = await call_downstream("/work", params={"ms": ms, "fail": fail})
    return {
        "sent_trace_id": sent,
        "downstream": result,
        "echo_matches": result.get("trace_id") == sent,
    }


@app.post("/jobs")
async def schedule_job(ms: int = 50, fail: bool = False) -> dict:
    """Schedule a background job that stays observable after the response."""

    async def job() -> str:
        with timed_stage("job-work"):
            await asyncio.sleep(ms / 1000)
        if fail:
            raise RuntimeError("background job failed")
        return f"job done after {ms}ms"

    run_observed("demo-job", job)
    return {"scheduled": True, "trace_id": current_trace_id()}


@app.get("/traces/{trace_id}")
async def replay_trace(trace_id: str) -> JSONResponse:
    """Merge every recorded segment of one trace id into a timeline."""
    timeline = build_timeline(trace_id)
    if timeline is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "trace not found or already evicted",
                     "trace_id": trace_id, "found": False},
        )
    return JSONResponse(content={"found": True, **timeline})


@app.get("/metrics")
def metrics() -> dict:
    snap = metrics_registry.snapshot()
    return {
        "total": snap.total,
        "errors": snap.errors,
        "slow": snap.slow,
        "sampled_out": snap.sampled_out,
        "background_total": snap.background_total,
        "background_errors": snap.background_errors,
    }


@app.get("/config")
def get_config() -> dict:
    s = settings_holder.get()
    return {
        "slow_request_ms": s.slow_request_ms,
        "sample_rate": sampling_policy.rate,
        "log_dir": s.log_dir,
        "rotation_max_bytes": s.rotation_max_bytes,
        "rotation_backup_count": s.rotation_backup_count,
        "service_name": s.service_name,
        "downstream_url": s.downstream_url,
        "segment_max_traces": s.segment_max_traces,
        "segment_max_per_trace": s.segment_max_per_trace,
    }


class ConfigUpdate(BaseModel):
    slow_request_ms: float | None = None
    sample_rate: float | None = None
    downstream_url: str | None = None
    segment_max_traces: int | None = None
    segment_max_per_trace: int | None = None


@app.post("/config")
def update_config(update: ConfigUpdate) -> dict:
    changes = {}
    for key in ("slow_request_ms", "downstream_url",
                "segment_max_traces", "segment_max_per_trace"):
        value = getattr(update, key)
        if value is not None:
            changes[key] = value
    if changes:
        settings_holder.update(**changes)
    s = settings_holder.get()
    segment_store.configure(s.segment_max_traces, s.segment_max_per_trace)
    if update.sample_rate is not None:
        sampling_policy.set_rate(update.sample_rate)
    return get_config()


if __name__ == "__main__":
    import sys

    if "--with-downstream" in sys.argv:
        import downstream

        downstream.run_server(port=8001)
        print("downstream listening on 127.0.0.1:8001")
    uvicorn.run(app, host="127.0.0.1", port=8000)
