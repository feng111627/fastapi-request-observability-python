"""Structured observability events shared by all segments of a trace.

Every emitted line is one *segment* of a trace: the entry request, a
downstream call, or a background task. Lines go to the same rotating
JSON log, carry a monotonic ``seq`` and an epoch-ms ``ts_ms`` so a
replay can merge them into one timeline without relying on the (second
precision) formatted timestamp.
"""
from __future__ import annotations

import threading
import time

from app.config import settings_holder
from app.context import current_trace_id
from app.logging_setup import get_request_logger
from app.replay import trace_replayer

_seq_lock = threading.Lock()
_seq = 0


def next_seq() -> int:
    """Return a process-wide monotonically increasing event sequence number."""
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def reset_seq() -> None:
    """Reset the sequence counter (tests)."""
    global _seq
    with _seq_lock:
        _seq = 0


def emit_event(msg: str, **fields: object) -> None:
    """Write one structured event line belonging to the current trace.

    ``trace_id`` defaults to the ambient trace; ``seq`` and ``ts_ms``
    are stamped automatically.
    """
    settings = settings_holder.get()
    fields.setdefault("trace_id", current_trace_id())
    fields["seq"] = next_seq()
    fields["ts_ms"] = round(time.time() * 1000, 3)
    logger = get_request_logger(
        settings.log_dir,
        settings.log_file,
        settings.rotation_max_bytes,
        settings.rotation_backup_count,
    )
    logger.info(msg, extra={"trace": fields})
    # Record the id only now that a line was really written: the replay
    # "seen" set is evidence that on-disk records existed, so an id
    # missing from the files means rotation (not a never-seen request).
    trace_id = fields.get("trace_id")
    if isinstance(trace_id, str) and trace_id != "-":
        trace_replayer.note_seen(trace_id)
