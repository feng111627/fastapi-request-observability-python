"""Replay under sampling: sampled-out traces stay replayable, reasons are
correct, concurrent traces never cross, and sampling semantics are unchanged."""
import os
import threading
import time

from fastapi.testclient import TestClient

from app.metrics import metrics_registry
from app.sampling import sampling_policy
from main import app
from tests.conftest import read_logs


def test_sampled_out_chain_is_fully_replayable(client):
    """rate=0: a normal request that called downstream and scheduled a
    background task is dropped from the main log, yet its whole trace —
    entry, downstream call, background task — replays by id."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tid = "sampled-chain-0001"
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
    assert "entry" in kinds
    assert "downstream" in kinds
    assert "background" in kinds
    summary = rep["summary"]
    assert summary["total_duration_ms"] is not None
    assert summary["downstream_calls"] == 1
    assert summary["background_tasks"] == 1
    # sampling semantics unchanged: nothing of this trace in the main log,
    # and the dropped normal requests are still counted as sampled out
    assert all(tid not in l.get("trace_id", "") for l in read_logs(log_dir))
    assert metrics_registry.snapshot().sampled_out >= 2


def test_cross_service_segments_queryable_with_sampling_on(client):
    """rate=0: the downstream service's own entry under the same trace id
    is still found by replay, so the cross-service line does not break."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tid = "sampled-cross-0001"
    r = c.get("/chain", headers={"X-Trace-Id": tid})
    assert r.status_code == 200

    rep = c.get(f"/replay/{tid}").json()
    assert rep["found"] is True
    entries = [s for s in rep["timeline"] if s["kind"] == "entry"]
    services = {s["service"] for s in entries}
    assert "upstream" in services
    assert "downstream" in services  # downstream's own trace record survives
    downstream = [s for s in rep["timeline"] if s["kind"] == "downstream"]
    assert len(downstream) == 1
    assert downstream[0]["trace_sent"] == tid
    assert downstream[0]["trace_received"] == tid
    assert downstream[0]["matched"] is True


def test_sampled_out_request_is_not_reported_as_rotated(client):
    """A sampled-out request must replay as found — never as rotated_away —
    and an id that never existed must say unknown."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tid = "sampled-plain-0001"
    c.get("/", headers={"X-Trace-Id": tid})

    rep = c.get(f"/replay/{tid}").json()
    assert rep["found"] is True  # no rotation happened; the trace is intact
    assert [s["kind"] for s in rep["timeline"]] == ["entry"]

    missing = c.get("/replay/never-existed-0001").json()
    assert missing["found"] is False
    assert missing["reason"] == "unknown"


def test_rotated_away_only_when_rotation_really_happened(client):
    """rotated_away is reported only for an id whose records were really
    deleted by rotation; unknown ids keep saying unknown."""
    c, log_dir = client
    from app.config import settings_holder
    from app.logging_setup import reset_loggers

    tid = "sampled-rotate-0001"
    c.get("/", headers={"X-Trace-Id": tid})  # rate 1.0: written to main log
    assert c.get(f"/replay/{tid}").json()["found"] is True

    settings_holder.update(rotation_max_bytes=600, rotation_backup_count=1)
    reset_loggers()
    present = True
    for i in range(200):
        c.get("/", headers={"X-Trace-Id": f"flood-{i:06d}-aaaaaa"})
        present = any(
            tid in open(os.path.join(str(log_dir), name)).read()
            for name in os.listdir(str(log_dir))
            if name.startswith("requests.log")
        )
        if not present:
            break
    assert not present, "test setup: expected tid to be rotated away"

    rep = c.get(f"/replay/{tid}").json()
    assert rep["found"] is False
    assert rep["reason"] == "rotated_away"
    missing = c.get("/replay/never-existed-0002").json()
    assert missing["reason"] == "unknown"


def test_errors_and_slow_kept_and_replayable_with_sampling(client):
    """rate=0: errors and slow requests are still kept in the main log and
    replayable; only normal requests are counted as sampled out."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    from app.config import settings_holder

    settings_holder.update(slow_request_ms=100.0)
    c.get("/")                                                   # normal -> dropped
    c.get("/slow", params={"ms": 150}, headers={"X-Trace-Id": "sampled-slow-0001"})
    c.get("/crash", headers={"X-Trace-Id": "sampled-crash-001"})

    for tid, expect in (("sampled-slow-0001", True), ("sampled-crash-001", True)):
        rep = c.get(f"/replay/{tid}").json()
        assert rep["found"] is True
        assert rep["summary"]["total_duration_ms"] is not None
    assert c.get("/replay/sampled-slow-0001").json()["summary"]["slow"] is True
    assert "uncaught" in c.get("/replay/sampled-crash-001").json()["summary"]["error_kinds"]

    logged_ids = {l["trace_id"] for l in read_logs(log_dir)}
    assert "sampled-slow-0001" in logged_ids   # kept in the main log
    assert "sampled-crash-001" in logged_ids
    snap = metrics_registry.snapshot()
    assert snap.sampled_out >= 1               # the normal request was dropped
    assert snap.errors.get("uncaught") == 1


def test_concurrent_sampled_traces_replay_without_crossing(client):
    """rate=0 with many concurrent chains: each id replays exactly its own
    entry/downstream segments, never another request's."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    n = 12
    errors = []

    def hit(i):
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                tid = f"sampled-conc-{i:04d}-ab"
                r = tc.get("/chain", headers={"X-Trace-Id": tid})
                assert r.status_code == 200, r.text
                assert r.json()["matched"] is True
        except AssertionError as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # all normal requests were sampled out (each /chain is counted twice:
    # once by the upstream middleware, once by the in-process downstream's);
    # none landed in the main log
    assert metrics_registry.snapshot().sampled_out == 2 * n
    assert read_logs(log_dir) == []

    for i in range(n):
        tid = f"sampled-conc-{i:04d}-ab"
        rep = c.get(f"/replay/{tid}").json()
        assert rep["found"] is True, tid
        kinds = [s["kind"] for s in rep["timeline"]]
        assert "entry" in kinds and "downstream" in kinds
        # every segment on this timeline carries this request's own id
        downstream = [s for s in rep["timeline"] if s["kind"] == "downstream"]
        assert all(s["trace_sent"] == tid for s in downstream)
        assert all(s["trace_received"] == tid for s in downstream)
        assert rep["summary"]["downstream_calls"] == 1
        assert rep["summary"]["downstream_mismatches"] == 0
