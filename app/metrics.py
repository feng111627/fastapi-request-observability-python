"""In-memory request metrics consistent with log conclusions."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class MetricsSnapshot:
    total: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    slow: int = 0
    sampled_out: int = 0


class MetricsRegistry:
    """Thread-safe counters keyed by error kind and slow flag.

    ``record`` is called exactly once per request with the same
    classification that was (or would have been) written to the log,
    so metrics and logs always agree on a request's conclusion.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = MetricsSnapshot()

    def record(self, *, error_kind: str, is_slow: bool, logged: bool) -> None:
        with self._lock:
            self._snapshot.total += 1
            if error_kind != "none":
                self._snapshot.errors[error_kind] = self._snapshot.errors.get(error_kind, 0) + 1
            if is_slow:
                self._snapshot.slow += 1
            if not logged:
                self._snapshot.sampled_out += 1

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                total=self._snapshot.total,
                errors=dict(self._snapshot.errors),
                slow=self._snapshot.slow,
                sampled_out=self._snapshot.sampled_out,
            )

    def reset(self) -> None:
        with self._lock:
            self._snapshot = MetricsSnapshot()


metrics_registry = MetricsRegistry()
