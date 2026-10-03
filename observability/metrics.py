"""进程内指标：与日志共用同一条请求记录，保证结论一致。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class RequestRecord:
    """单个请求的完整观测结论，日志与指标都从这里取数。"""

    trace_id: str
    method: str
    path: str
    status_code: int
    duration_ms: float
    stage_ms: dict[str, float] = field(default_factory=dict)
    error_class: str = "none"
    is_slow: bool = False
    sampled: bool = True


class MetricsRegistry:
    """按 (method, path, status, error_class, is_slow) 维度计数。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, int] = {}
        self._total = 0
        self._slow = 0
        self._errors = 0

    def record(self, rec: RequestRecord) -> None:
        key = (rec.method, rec.path, rec.status_code, rec.error_class, rec.is_slow)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            self._total += 1
            if rec.is_slow:
                self._slow += 1
            if rec.error_class != "none":
                self._errors += 1

    def snapshot(self) -> dict:
        with self._lock:
            requests = [
                {
                    "method": k[0],
                    "path": k[1],
                    "status_code": k[2],
                    "error_class": k[3],
                    "is_slow": k[4],
                    "count": v,
                }
                for k, v in sorted(self._counters.items())
            ]
            return {
                "total": self._total,
                "slow": self._slow,
                "errors": self._errors,
                "requests": requests,
            }
