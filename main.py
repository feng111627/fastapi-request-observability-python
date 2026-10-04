"""Demo service wiring the observability stack together."""
from __future__ import annotations

import asyncio
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings_holder
from app.context import current_trace_id, mark_error, timed_stage
from app.downstream import downstream_app
from app.errors import BusinessError, ErrorKind, OperationTimeout, TracePropagationError
from app.metrics import metrics_registry
from app.middleware import TraceMiddleware
from app.propagate import trace_headers
from app.replay import trace_replayer
from app.sampling import sampling_policy
from app.tasks import run_in_background, task_result_store
from app import traced_http

app = FastAPI(title="observability-demo", lifespan=None)
app.add_middleware(TraceMiddleware)


@app.on_event("startup")
async def _startup() -> None:
    # Local, offline reproduction: the "downstream service" is reached
    # over an isolated in-process ASGI transport. Set
    # DOWNSTREAM_BASE_URL to call a separately started process instead.
    base = os.environ.get("DOWNSTREAM_BASE_URL")
    if base:
        traced_http.configure_clients(downstream_app, real_base_url=base)
    else:
        traced_http.configure_clients(downstream_app)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await traced_http.aclose_http()


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


@app.exception_handler(TracePropagationError)
async def propagation_handler(request: Request, exc: TracePropagationError) -> JSONResponse:
    mark_error(ErrorKind.PROPAGATION.value)
    return JSONResponse(
        status_code=502,
        content={
            "detail": str(exc),
            "error_kind": ErrorKind.PROPAGATION.value,
            "trace_id": current_trace_id(),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    mark_error(ErrorKind.VALIDATION.value)
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "trace_id": current_trace_id()},
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
async def background(fail: bool = False) -> dict:
    """Schedule a task that keeps running after the response and is observed."""

    async def work() -> str:
        with timed_stage("background-work"):
            await asyncio.sleep(0.02)
        if fail:
            raise RuntimeError("background task failed on purpose")
        return f"processed for {current_trace_id()}"

    run_in_background(work, name="demo-background")
    return {"scheduled": True, "trace_id": current_trace_id()}


@app.get("/tasks/{trace_id}")
def get_task_results(trace_id: str) -> dict:
    """Background-task outcomes attributable to one request."""
    results = task_result_store.get(trace_id)
    return {
        "trace_id": trace_id,
        "found": bool(results),
        "count": len(results),
        "results": [r.as_event() for r in results],
    }


@app.get("/outbound")
async def outbound() -> dict:
    # In a real service these headers go onto the httpx/requests call.
    return {"would_send_headers": trace_headers()}


@app.get("/chain")
async def chain(lie: bool = False, work_ms: int = 5) -> dict:
    """Cross-service scenario: really call the local downstream and verify."""
    trace_id = current_trace_id()
    with timed_stage("downstream-echo"):
        call = await traced_http.call_downstream("/echo", lie=lie, timeout=5.0)
    return {
        "trace_id": trace_id,
        "downstream_url": call.url,
        "downstream_status": call.status_code,
        "trace_sent": call.trace_sent,
        "trace_received": call.trace_received,
        "matched": call.matched,
        "downstream_duration_ms": call.duration_ms,
        "downstream_body": call.body,
    }


@app.get("/replay/{trace_id}")
def replay(trace_id: str) -> dict:
    """Replay the whole trace (entry + downstream + background) by id."""
    return trace_replayer.replay(trace_id)


@app.get("/metrics")
def metrics() -> dict:
    snap = metrics_registry.snapshot()
    return {
        "total": snap.total,
        "errors": snap.errors,
        "slow": snap.slow,
        "sampled_out": snap.sampled_out,
        "background_total": snap.background_total,
        "background_failed": snap.background_failed,
        "trace_mismatches": snap.trace_mismatches,
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
    }


class ConfigUpdate(BaseModel):
    slow_request_ms: float | None = None
    sample_rate: float | None = None


@app.post("/config")
def update_config(update: ConfigUpdate) -> dict:
    changes = {}
    if update.slow_request_ms is not None:
        changes["slow_request_ms"] = update.slow_request_ms
    if changes:
        settings_holder.update(**changes)
    if update.sample_rate is not None:
        sampling_policy.set_rate(update.sample_rate)
    return get_config()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
