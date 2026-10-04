"""Bounded in-memory store of trace segments for cross-stage replay."""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Segment:
    """One observable fragment of a trace (request / downstream / background)."""

    kind: str  # "request" | "downstream" | "background"
    service: str
    name: str
    trace_id: str
    started_at: float  # epoch seconds
    duration_ms: float
    error_kind: str = "none"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "service": self.service,
            "name": self.name,
            "started_at": round(self.started_at, 6),
            "duration_ms": round(self.duration_ms, 3),
            "error_kind": self.error_kind,
            "detail": self.detail,
        }


class SegmentStore:
    """Thread-safe bounded store keyed by trace id.

    Bounded on two axes so a single busy trace (or a flood of traces)
    cannot grow memory without limit:

    - ``max_traces``: when exceeded, the oldest trace (with all its
      segments) is evicted; replaying it later reports "not found".
    - ``max_segments_per_trace``: when a single trace exceeds this,
      further segments are dropped and the trace is flagged truncated,
      so replay responses stay bounded.
    """

    def __init__(self, max_traces: int = 1000, max_segments_per_trace: int = 100) -> None:
        if max_traces < 1 or max_segments_per_trace < 1:
            raise ValueError("segment store limits must be >= 1")
        self._lock = threading.Lock()
        self._max_traces = max_traces
        self._max_segments = max_segments_per_trace
        self._traces: OrderedDict[str, list[Segment]] = OrderedDict()
        self._truncated: set[str] = set()

    def configure(self, max_traces: int, max_segments_per_trace: int) -> None:
        """Adjust limits at runtime; eviction applies to subsequent adds."""
        if max_traces < 1 or max_segments_per_trace < 1:
            raise ValueError("segment store limits must be >= 1")
        with self._lock:
            self._max_traces = max_traces
            self._max_segments = max_segments_per_trace

    @property
    def limits(self) -> tuple[int, int]:
        with self._lock:
            return self._max_traces, self._max_segments

    def add(self, segment: Segment) -> None:
        with self._lock:
            segments = self._traces.get(segment.trace_id)
            if segments is None:
                if len(self._traces) >= self._max_traces:
                    evicted_id, _ = self._traces.popitem(last=False)
                    self._truncated.discard(evicted_id)
                segments = []
                self._traces[segment.trace_id] = segments
            else:
                self._traces.move_to_end(segment.trace_id)
            if len(segments) >= self._max_segments:
                self._truncated.add(segment.trace_id)
                return
            segments.append(segment)

    def timeline(self, trace_id: str) -> tuple[list[Segment], bool] | None:
        """Segments for one trace ordered by start time, plus truncated flag.

        Returns ``None`` when the trace is unknown (never seen or evicted).
        """
        with self._lock:
            segments = self._traces.get(trace_id)
            if segments is None:
                return None
            ordered = sorted(segments, key=lambda s: s.started_at)
            return ordered, trace_id in self._truncated

    def reset(self) -> None:
        with self._lock:
            self._traces.clear()
            self._truncated.clear()


segment_store = SegmentStore()
