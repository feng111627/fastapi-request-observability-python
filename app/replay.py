"""Replay a whole trace by id.

Given a trace id, merge every segment belonging to it — the entry
request, downstream calls and background tasks — from the JSON log
files into one timeline. Two logs are searched: the main log (kept
requests) and the replay-buffer log (segments of sampled-out requests),
each with their retained rotation backups.

Unknown ids and ids whose records have been rotated away are reported
explicitly (never an error/500). A trace id counts as "seen" only when
a segment was actually written to one of the logs, so a request that
was merely sampled out is never misreported as rotated away. Segment
counts, stage counts and field lengths are capped so a trace with very
many segments cannot blow up memory or the response.
"""
from __future__ import annotations

import glob
import json
import os
import threading
from collections import OrderedDict

from app.config import settings_holder
from app.logging_setup import replay_log_file


class ReplayLimits:
    """Boundaries applied when building one trace's timeline."""

    def __init__(
        self,
        max_segments: int = 200,
        max_stages: int = 50,
        max_field_chars: int = 512,
        max_files: int = 10,
    ) -> None:
        self.max_segments = max_segments
        self.max_stages = max_stages
        self.max_field_chars = max_field_chars
        self.max_files = max_files


def _truncate(value: object, limit: int) -> object:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit] + f"...<+{len(value) - limit} chars>"


class TraceReplayer:
    """Builds a merged timeline for one trace id from the JSON log files."""

    def __init__(self, limits: ReplayLimits | None = None, seen_cap: int = 10_000) -> None:
        self.limits = limits or ReplayLimits()
        self._seen_lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = seen_cap

    def note_seen(self, trace_id: str) -> None:
        """Record that a trace id was emitted in this process (rotation hint)."""
        with self._seen_lock:
            self._seen.pop(trace_id, None)
            self._seen[trace_id] = None
            while len(self._seen) > self._seen_cap:
                self._seen.popitem(last=False)

    def has_seen(self, trace_id: str) -> bool:
        with self._seen_lock:
            return trace_id in self._seen

    def reset(self) -> None:
        with self._seen_lock:
            self._seen.clear()

    def _log_files(self, log_dir: str, log_file: str) -> list[str]:
        # Main log plus the replay-buffer log (sampled-out requests),
        # each with their own rotation backups; capped per pattern.
        paths: list[str] = []
        for name in (log_file, replay_log_file(log_file)):
            matches = sorted(
                glob.glob(os.path.join(log_dir, name + "*")),
                key=lambda p: (os.path.getmtime(p), p),
                reverse=True,
            )
            paths.extend(matches[: self.limits.max_files])
        return paths

    def _read_segments(self, trace_id: str, log_dir: str, log_file: str):
        segments: list[dict] = []
        truncated = 0
        for path in self._log_files(log_dir, log_file):
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or trace_id not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("trace_id") != trace_id:
                        continue
                    segment = self._to_segment(event)
                    if segment is None:
                        continue
                    if len(segments) >= self.limits.max_segments:
                        truncated += 1
                        continue
                    segments.append(segment)
        return segments, truncated

    def _to_segment(self, event: dict) -> dict | None:
        msg = event.get("msg")
        if msg == "request":
            kind = "entry"
        elif msg == "downstream_call":
            kind = "downstream"
        elif msg == "background_task":
            kind = "background"
        else:
            return None
        stages = event.get("stages") or []
        stage_truncated = 0
        if len(stages) > self.limits.max_stages:
            stage_truncated = len(stages) - self.limits.max_stages
            stages = stages[: self.limits.max_stages]
        segment = {
            "kind": kind,
            "seq": event.get("seq", 0),
            "ts_ms": event.get("ts_ms", 0.0),
            "service": event.get("service", "upstream"),
        }
        if kind == "entry":
            segment.update(
                method=event.get("method"),
                path=event.get("path"),
                status=event.get("status"),
                duration_ms=event.get("duration_ms"),
                slow=event.get("slow"),
                error_kind=event.get("error_kind", "none"),
                stages=stages,
            )
        elif kind == "downstream":
            segment.update(
                url=event.get("url"),
                status=event.get("downstream_status"),
                duration_ms=event.get("duration_ms"),
                trace_sent=event.get("trace_sent"),
                trace_received=event.get("trace_received"),
                matched=event.get("matched"),
            )
        else:
            segment.update(
                name=event.get("name"),
                status_text=event.get("status"),
                duration_ms=event.get("duration_ms"),
                error_kind=event.get("error_kind", "none"),
                error=_truncate(event.get("error", ""), self.limits.max_field_chars),
                result=_truncate(event.get("result", ""), self.limits.max_field_chars),
            )
        if stage_truncated:
            segment["stages_truncated"] = stage_truncated
        return segment

    def replay(self, trace_id: str) -> dict:
        """Return the merged timeline, or an explicit not-found result.

        ``found`` is False for unknown ids and for ids whose segments
        were rotated away; ``reason`` distinguishes ``unknown`` from
        ``rotated_away``.
        """
        settings = settings_holder.get()
        segments, truncated = self._read_segments(
            trace_id, settings.log_dir, settings.log_file
        )
        if not segments:
            reason = "rotated_away" if self.has_seen(trace_id) else "unknown"
            return {
                "trace_id": trace_id,
                "found": False,
                "reason": reason,
                "segments": [],
                "timeline": [],
            }

        segments.sort(key=lambda s: (s["ts_ms"], s["seq"]))
        summary = self._summarize(segments, truncated)
        return {
            "trace_id": trace_id,
            "found": True,
            "segments_returned": len(segments),
            "segments_truncated": truncated,
            "limits": {
                "max_segments": self.limits.max_segments,
                "max_stages": self.limits.max_stages,
                "max_field_chars": self.limits.max_field_chars,
            },
            "summary": summary,
            "timeline": segments,
        }

    def _summarize(self, segments: list[dict], truncated: int) -> dict:
        entries = [s for s in segments if s["kind"] == "entry"]
        downstream = [s for s in segments if s["kind"] == "downstream"]
        background = [s for s in segments if s["kind"] == "background"]

        # The upstream entry owns the request's total duration; when a
        # downstream service logged its own entry under the same id,
        # prefer the upstream one and fall back to the latest entry.
        upstream_entries = [s for s in entries if s.get("service") == "upstream"]
        entry = upstream_entries[-1] if upstream_entries else (
            entries[-1] if entries else None
        )
        total_ms = entry["duration_ms"] if entry else None
        error_kinds = sorted(
            {
                s.get("error_kind")
                for s in segments
                if s.get("error_kind") and s.get("error_kind") != "none"
            }
        )
        slow_stage = None
        slowest: tuple[str, float] | None = None
        if entry:
            for st in entry.get("stages", []):
                if slowest is None or st["duration_ms"] > slowest[1]:
                    slowest = (st["name"], st["duration_ms"])
        if slowest is not None:
            slow_stage = {"name": slowest[0], "duration_ms": slowest[1]}

        mismatched = [s for s in downstream if s.get("matched") is False]
        failed_tasks = [s for s in background if s.get("status_text") == "failed"]
        return {
            "total_duration_ms": total_ms,
            "slow": entry.get("slow") if entry else None,
            "entry_error_kind": entry.get("error_kind") if entry else None,
            "error_kinds": error_kinds,
            "slowest_stage": slow_stage,
            "downstream_calls": len(downstream),
            "downstream_mismatches": len(mismatched),
            "background_tasks": len(background),
            "background_failed": len(failed_tasks),
            "segments_truncated": truncated,
        }


trace_replayer = TraceReplayer()
