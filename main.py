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
from app.errors import BusinessError, ErrorKind, OperationTimeout
from app.context import mark_error
from app.metrics import metrics_registry
from app.middleware import TraceMiddleware
from app.propagate import run_in_background, trace_headers
from app.sampling import sampling_policy

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


@app.get("/metrics")
def metrics() -> dict:
    snap = metrics_registry.snapshot()
    return {
        "total": snap.total,
        "errors": snap.errors,
        "slow": snap.slow,
        "sampled_out": snap.sampled_out,
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
