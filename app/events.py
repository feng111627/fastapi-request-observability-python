"""Structured observability events shared by all segments of a trace.

Every emitted line is one *segment* of a trace: the entry request, a
downstream call, or a background task. Lines go to the same rotating
JSON log, carry a monotonic ``seq`` and an epoch-ms ``ts_ms`` so a
replay can merge them into one timeline without relying on the (second
precision) formatted timestamp.

Segments of requests the sampler dropped are not lost: they go to a
separate replay-buffer log (``<name>.replay.log``, also rotating) so a
trace that really happened can always be replayed by id, while the main
log and the sampling metrics keep their exact meaning.
"""
from __future__ import annotations

import threading
import time

from app.config import settings_holder
from app.context import current_trace_id, get_trace
from app.logging_setup import get_request_logger, replay_log_file
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
    are stamped automatically. Segments of a sampled-out request are
    routed to the replay-buffer log instead of the main log. Every line
    actually written marks its trace id as seen, so replay can tell
    "id never existed" apart from "id's records were rotated away".
    """
    settings = settings_holder.get()
    fields.setdefault("trace_id", current_trace_id())
    fields["seq"] = next_seq()
    fields["ts_ms"] = round(time.time() * 1000, 3)
    ctx = get_trace()
    sampled_out = ctx.sampled_out if ctx is not None else False
    log_file = settings.log_file if not sampled_out else replay_log_file(settings.log_file)
    logger = get_request_logger(
        settings.log_dir,
        log_file,
        settings.rotation_max_bytes,
        settings.rotation_backup_count,
    )
    logger.info(msg, extra={"trace": fields})
    trace_id = fields.get("trace_id")
    if isinstance(trace_id, str) and trace_id and trace_id != "-":
        trace_replayer.note_seen(trace_id)
