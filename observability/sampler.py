"""采样策略：错误/慢请求必保留，其余按 trace_id 哈希确定性采样。"""
from __future__ import annotations

import hashlib

from .config import ConfigStore


class Sampler:
    """每次决策实时读取配置快照，因此运行中改采样率只影响之后的请求。"""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store

    def should_keep(self, trace_id: str, *, is_error: bool, is_slow: bool) -> bool:
        # 错误链路与慢请求链路永远保留，保证代表性
        if is_error or is_slow:
            return True
        rate = self._store.get().sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        # 对 trace_id 做哈希，同一 trace_id 决策稳定，便于测试与排查
        digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") / 2**64
        return bucket < rate
