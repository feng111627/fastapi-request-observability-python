"""对外调用辅助：把当前 trace_id 注入出站请求头。"""
from __future__ import annotations

from .config import ConfigStore
from .context import get_trace_id


def outbound_headers(store: ConfigStore) -> dict[str, str]:
    """返回携带当前 trace_id 的请求头，用于 httpx 等出站调用。

    没有活动链路时返回空 dict，不会伪造标识。
    """
    trace_id = get_trace_id()
    if trace_id is None:
        return {}
    return {store.get().trace_header: trace_id}
