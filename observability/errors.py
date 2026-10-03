"""异常分类与 FastAPI 异常处理器。

分类结论通过 request.state.error_class 写回请求作用域，
中间件据此生成与日志一致的 RequestRecord。
"""
from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .context import get_trace_id


class ErrorClass(str, Enum):
    NONE = "none"
    BUSINESS = "business"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    UNCAUGHT = "uncaught"


class BusinessError(Exception):
    """业务异常：可预期、有业务错误码，返回 4xx。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _payload(request: Request, error_class: ErrorClass, **extra: object) -> dict:
    # 优先取 contextvar；未捕获异常场景下 contextvar 已复位，兜底取 scope state
    trace_id = get_trace_id() or getattr(request.state, "trace_id", None)
    return {
        "error_class": error_class.value,
        "trace_id": trace_id,
        **extra,
    }


def register_exception_handlers(app: FastAPI) -> None:
    """向 FastAPI 应用注册分类异常处理器。"""

    @app.exception_handler(BusinessError)
    async def business_handler(request: Request, exc: BusinessError) -> JSONResponse:
        request.state.error_class = ErrorClass.BUSINESS.value
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(request, ErrorClass.BUSINESS, code=exc.code, message=exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request.state.error_class = ErrorClass.VALIDATION.value
        return JSONResponse(
            status_code=422,
            content=_payload(request, ErrorClass.VALIDATION, detail=exc.errors()),
        )

    @app.exception_handler(TimeoutError)
    async def timeout_handler(request: Request, exc: TimeoutError) -> JSONResponse:
        request.state.error_class = ErrorClass.TIMEOUT.value
        return JSONResponse(
            status_code=504,
            content=_payload(request, ErrorClass.TIMEOUT, message=str(exc) or "timeout"),
        )

    @app.exception_handler(Exception)
    async def uncaught_handler(request: Request, exc: Exception) -> JSONResponse:
        request.state.error_class = ErrorClass.UNCAUGHT.value
        return JSONResponse(
            status_code=500,
            content=_payload(request, ErrorClass.UNCAUGHT, message=str(exc)),
        )
