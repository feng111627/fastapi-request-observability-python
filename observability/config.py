"""可观测性配置：慢请求阈值、采样率、日志轮转等，支持运行中安全变更。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ObservabilityConfig:
    """不可变配置快照；运行中通过整体替换实现可预期的变更。"""

    slow_threshold_ms: float = 500.0
    sample_rate: float = 1.0  # 0.0 ~ 1.0，仅作用于非错误、非慢请求
    trace_header: str = "X-Request-ID"
    log_file: str = "logs/app.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5


class ConfigStore:
    """线程安全的配置持有者：读取拿到的是一致快照，写入整体替换。"""

    def __init__(self, config: ObservabilityConfig | None = None) -> None:
        self._lock = threading.Lock()
        self._config = config or ObservabilityConfig()

    def get(self) -> ObservabilityConfig:
        with self._lock:
            return self._config

    def update(self, **changes: object) -> ObservabilityConfig:
        with self._lock:
            self._config = replace(self._config, **changes)
            return self._config
