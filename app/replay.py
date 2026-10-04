"""Replay: merge all segments of one trace id into a single timeline."""
from __future__ import annotations

from typing import Any

from app.errors import ErrorKind
from app.segments import segment_store


def build_timeline(trace_id: str) -> dict[str, Any] | None:
    """Timeline for one trace id, or ``None`` if unknown/evicted.

    Segments come from the bounded in-memory store, so the response is
    naturally capped: at most ``max_segments_per_trace`` entries, with
    ``truncated=True`` when overflow was dropped.
    """
    found = segment_store.timeline(trace_id)
    if found is None:
        return None
    segments, truncated = found

    start = min(s.started_at for s in segments)
    end = max(s.started_at + s.duration_ms / 1000 for s in segments)
    slowest = max(segments, key=lambda s: s.duration_ms)
    errors = [
        {"kind": s.error_kind, "service": s.service, "name": s.name}
        for s in segments
        if s.error_kind != ErrorKind.NONE.value
    ]
    counts: dict[str, int] = {}
    for s in segments:
        counts[s.kind] = counts.get(s.kind, 0) + 1

    max_traces, max_per_trace = segment_store.limits
    return {
        "trace_id": trace_id,
        "segment_count": len(segments),
        "segment_counts": counts,
        "truncated": truncated,
        "limits": {"max_traces": max_traces, "max_segments_per_trace": max_per_trace},
        "started_at": round(start, 6),
        "total_duration_ms": round((end - start) * 1000, 3),
        "slowest_segment": {
            "kind": slowest.kind,
            "service": slowest.service,
            "name": slowest.name,
            "duration_ms": round(slowest.duration_ms, 3),
        },
        "errors": errors,
        "timeline": [s.as_dict() for s in segments],
    }
