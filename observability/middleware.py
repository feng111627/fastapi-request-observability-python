"""ASGI 中间件：注入/生成 trace_id、分阶段计时、异常分类、采样与日志。

实现要点：
- 纯 ASGI 中间件（非 BaseHTTPMiddleware），contextvars 在同一 task 内
  自然传播到路由处理、后台任务；每个请求独立 task，互不串号。
- 日志与指标消费同一条 RequestRecord，结论必然一致。
"""
from __future__ import annotations

import time

from .config import ConfigStore
from .context import new_trace_id, reset_trace_id, sanitize_incoming, set_trace_id
from .errors import ErrorClass
from .logging_setup import get_logger
from .metrics import MetricsRegistry, RequestRecord
from .sampler import Sampler


class TraceMiddleware:
    def __init__(
        self,
        app,
        store: ConfigStore,
        sampler: Sampler,
        metrics: MetricsRegistry,
    ) -> None:
        self.app = app
        self.store = store
        self.sampler = sampler
        self.metrics = metrics

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cfg = self.store.get()  # 请求开始时取一次快照，本次请求内行为稳定
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        incoming = sanitize_incoming(headers.get(cfg.trace_header.lower()))
        trace_id = incoming or new_trace_id()
        token = set_trace_id(trace_id)
        # 写入 scope state：未捕获异常被外层 ServerErrorMiddleware 处理时，
        # contextvar 已复位，异常处理器可从这里兜底取到 trace_id
        scope.setdefault("state", {})["trace_id"] = trace_id

        status_code = 500
        send_ms = 0.0
        receive_ms = 0.0

        async def timed_receive():  # type: ignore[no-untyped-def]
            nonlocal receive_ms
            t = time.perf_counter()
            try:
                return await receive()
            finally:
                receive_ms += (time.perf_counter() - t) * 1000

        async def timed_send(message):  # type: ignore[no-untyped-def]
            nonlocal status_code, send_ms
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append(
                    (cfg.trace_header.lower().encode(), trace_id.encode())
                )
            t = time.perf_counter()
            try:
                await send(message)
            finally:
                send_ms += (time.perf_counter() - t) * 1000

        start = time.perf_counter()
        try:
            await self.app(scope, timed_receive, timed_send)
        except Exception:
            # 未被任何处理器接住的异常（例如 ExceptionMiddleware 之外抛出）
            scope.setdefault("state", {})["error_class"] = ErrorClass.UNCAUGHT.value
            status_code = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            error_class = scope.get("state", {}).get(
                "error_class", ErrorClass.NONE.value
            )
            is_slow = duration_ms >= cfg.slow_threshold_ms
            is_error = error_class != ErrorClass.NONE.value or status_code >= 500
            rec = RequestRecord(
                trace_id=trace_id,
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status_code,
                duration_ms=round(duration_ms, 3),
                stage_ms={
                    "receive_ms": round(receive_ms, 3),
                    "app_ms": round(duration_ms - receive_ms - send_ms, 3),
                    "send_ms": round(send_ms, 3),
                },
                error_class=error_class,
                is_slow=is_slow,
                sampled=self.sampler.should_keep(
                    trace_id, is_error=is_error, is_slow=is_slow
                ),
            )
            # 指标全量记录（不受采样影响），日志按采样输出
            self.metrics.record(rec)
            if rec.sampled:
                get_logger().info(
                    {
                        "trace_id": rec.trace_id,
                        "method": rec.method,
                        "path": rec.path,
                        "status_code": rec.status_code,
                        "duration_ms": rec.duration_ms,
                        "stage_ms": rec.stage_ms,
                        "error_class": rec.error_class,
                        "is_slow": rec.is_slow,
                        "slow_threshold_ms": cfg.slow_threshold_ms,
                    }
                )
            reset_trace_id(token)
