import asyncio
import os

import httpx2 as httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Query

from observability import (
    BusinessError,
    ConfigStore,
    MetricsRegistry,
    ObservabilityConfig,
    Sampler,
    TraceMiddleware,
    bind_to_current_context,
    get_logger,
    get_trace_id,
    outbound_headers,
    register_exception_handlers,
    setup_logging,
)


def create_app(store: ConfigStore | None = None) -> FastAPI:
    """应用工厂：测试可注入独立配置与日志文件，互不干扰。"""
    store = store or ConfigStore(ObservabilityConfig())
    metrics = MetricsRegistry()
    sampler = Sampler(store)
    cfg = store.get()
    setup_logging(cfg.log_file, cfg.log_max_bytes, cfg.log_backup_count)

    app = FastAPI(title="request-observability-demo")
    app.add_middleware(
        TraceMiddleware, store=store, sampler=sampler, metrics=metrics
    )
    register_exception_handlers(app)
    app.state.config_store = store
    app.state.metrics = metrics

    @app.get("/")
    def read_root():
        return {"Hello": "World", "trace_id": get_trace_id()}

    @app.get("/items/{item_id}")
    def read_item(item_id: int, q: str = Query(min_length=3)):
        """参数校验演示：item_id 非 int 或 q 太短会触发 validation。"""
        return {"item_id": item_id, "q": q}

    @app.get("/biz")
    def biz(fail: bool = False):
        """业务异常演示。"""
        if fail:
            raise BusinessError(
                code="BIZ_NOT_ALLOWED", message="业务规则不允许", status_code=409
            )
        return {"ok": True}

    @app.get("/boom")
    def boom():
        """未捕获异常演示。"""
        raise RuntimeError("something exploded")

    @app.get("/timeout")
    async def timeout():
        """超时演示。"""
        await asyncio.sleep(0.01)
        raise TimeoutError("upstream deadline exceeded")

    @app.get("/slow")
    async def slow(ms: int = 600):
        """慢请求演示：默认 600ms，超过默认阈值 500ms。"""
        await asyncio.sleep(ms / 1000)
        return {"slept_ms": ms, "trace_id": get_trace_id()}

    @app.get("/bg")
    async def bg(background_tasks: BackgroundTasks):
        """后台任务演示：任务里拿到的 trace_id 与请求一致。"""
        tid = get_trace_id()

        def work():
            get_logger().info(
                {"event": "background_task", "trace_id": get_trace_id()}
            )

        background_tasks.add_task(bind_to_current_context(work))
        return {"trace_id": tid, "background": "scheduled"}

    @app.get("/outbound")
    async def outbound():
        """对外调用演示：把当前 trace_id 透传给下游（这里调用自身）。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/", headers=outbound_headers(store))
        return {
            "trace_id": get_trace_id(),
            "downstream_trace_id": resp.json().get("trace_id"),
        }

    @app.get("/metrics")
    def get_metrics():
        return metrics.snapshot()

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
