"""Replay by trace id: merged timeline, unknown, rotated-away, truncation."""
import json
import os
import time

from app.replay import ReplayLimits, trace_replayer
from tests.conftest import read_logs


def test_replay_merges_entry_downstream_and_background(client):
    c, log_dir = client
    tid = "replay-full-0001"
    c.get("/chain", headers={"X-Trace-Id": tid})
    c.get("/background", headers={"X-Trace-Id": tid})
    deadline = time.time() + 2
    rep = {}
    while time.time() < deadline:
        rep = c.get(f"/replay/{tid}").json()
        if any(s["kind"] == "background" for s in rep.get("timeline", ())):
            break
        time.sleep(0.01)

    assert rep["found"] is True
    kinds = [s["kind"] for s in rep["timeline"]]
    # upstream entry, downstream service's own entry, downstream call, task
    assert "entry" in kinds
    assert "downstream" in kinds
    assert "background" in kinds
    # timeline is globally ordered
    seqs = [s["seq"] for s in rep["timeline"]]
    assert seqs == sorted(seqs)

    summary = rep["summary"]
    assert summary["downstream_calls"] == 1
    assert summary["downstream_mismatches"] == 0
    assert summary["background_tasks"] == 1
    assert summary["background_failed"] == 0
    assert summary["total_duration_ms"] is not None
    # mismatch scenario marks error source
    r = c.get("/chain", params={"lie": True}, headers={"X-Trace-Id": tid + "x"})
    assert r.status_code == 502
    rep2 = c.get(f"/replay/{tid}x").json()
    assert "propagation" in rep2["summary"]["error_kinds"]


def test_replay_unknown_id_is_explicit_not_found(client):
    c, log_dir = client
    rep = c.get("/replay/does-not-exist-id-1234").json()
    assert rep["found"] is False
    assert rep["reason"] == "unknown"
    assert rep["timeline"] == []


def test_replay_rotated_away_is_explicit(client):
    c, log_dir = client
    from app.config import settings_holder
    from app.logging_setup import reset_loggers
    tid = "replay-rotate-0001"
    c.get("/", headers={"X-Trace-Id": tid})  # known to this process
    assert c.get(f"/replay/{tid}").json()["found"] is True

    # force aggressive rotation: handler params are fixed at creation,
    # so reset cached loggers first, then flood with unrelated traffic
    # until every line for tid is deleted from current + backup files.
    settings_holder.update(rotation_max_bytes=600, rotation_backup_count=1)
    reset_loggers()
    present = True
    for i in range(200):
        c.get("/", headers={"X-Trace-Id": f"flood-{i:06d}-aaaaaa"})
        present = False
        for name in os.listdir(str(log_dir)):
            if not name.startswith("requests.log"):
                continue
            with open(os.path.join(str(log_dir), name)) as fh:
                if tid in fh.read():
                    present = True
                    break
        if not present:
            break
    assert not present, "test setup: expected tid to be rotated away"
    rep = c.get(f"/replay/{tid}").json()
    assert rep["found"] is False
    assert rep["reason"] == "rotated_away"


def test_replay_truncates_when_a_trace_has_many_segments(client):
    c, log_dir = client
    tid = "replay-many-000001"
    for _ in range(10):
        r = c.get("/chain", headers={"X-Trace-Id": tid})
        assert r.status_code == 200
    original = trace_replayer.limits
    trace_replayer.limits = ReplayLimits(max_segments=5, max_stages=50)
    try:
        rep = c.get(f"/replay/{tid}").json()
    finally:
        trace_replayer.limits = original
    assert rep["found"] is True
    assert rep["segments_returned"] == 5
    assert rep["segments_truncated"] >= 5
    assert len(rep["timeline"]) == 5
    assert rep["limits"]["max_segments"] == 5


def test_replay_slowest_stage_and_slow_flag(client):
    c, log_dir = client
    r = c.get("/slow", params={"ms": 250}, headers={"X-Trace-Id": "replay-slow-0001"})
    assert r.status_code == 200
    rep = c.get("/replay/replay-slow-0001").json()
    assert rep["summary"]["slow"] is True
    assert rep["summary"]["slowest_stage"]["name"] == "heavy-compute"
    assert rep["summary"]["slowest_stage"]["duration_ms"] >= 200
