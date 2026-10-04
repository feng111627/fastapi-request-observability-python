"""Replay by trace id: merged timeline, boundaries, truncation, eviction."""
import time


def _wait_segment_count(c, trace_id, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/traces/{trace_id}")
        if r.status_code == 200 and r.json()["segment_count"] >= want:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"trace {trace_id} never reached {want} segments")


def test_full_chain_merges_into_one_timeline(client, downstream_server):
    c, _ = client
    base_url, _ = downstream_server
    from app.config import settings_holder
    settings_holder.update(downstream_url=base_url)

    tid = "replay-000000001"
    c.get("/relay", headers={"X-Trace-Id": tid})
    c.post("/jobs?ms=100", headers={"X-Trace-Id": tid})

    # 2 upstream requests + 1 downstream request + 1 downstream call + 1 job
    data = _wait_segment_count(c, tid, 5)
    assert data["found"] is True
    assert data["truncated"] is False
    assert data["segment_counts"] == {"request": 3, "downstream": 1, "background": 1}

    # timeline is ordered and total covers the slowest segment
    starts = [s["started_at"] for s in data["timeline"]]
    assert starts == sorted(starts)
    slowest = data["slowest_segment"]
    assert slowest["kind"] == "background"  # the 100ms job dominates
    assert data["total_duration_ms"] >= slowest["duration_ms"]
    assert data["errors"] == []


def test_unknown_trace_id_gives_explicit_404(client):
    c, _ = client
    r = c.get("/traces/never-existed-000")
    assert r.status_code == 404
    body = r.json()
    assert body == {"detail": "trace not found or already evicted",
                    "trace_id": "never-existed-000", "found": False}


def test_evicted_trace_reports_not_found(client):
    c, _ = client
    c.post("/config", json={"segment_max_traces": 2})
    for i in range(3):
        c.get("/", headers={"X-Trace-Id": f"evict-{i:010d}"})
    assert c.get("/traces/evict-0000000000").status_code == 404
    assert c.get("/traces/evict-0000000002").status_code == 200


def test_segment_overflow_is_truncated_not_unbounded(client):
    c, _ = client
    c.post("/config", json={"segment_max_per_trace": 2})
    tid = "replay-000000002"
    # each request adds a request segment and (for /jobs) a background one
    c.post("/jobs?ms=10", headers={"X-Trace-Id": tid})
    c.post("/jobs?ms=10", headers={"X-Trace-Id": tid})
    c.post("/jobs?ms=10", headers={"X-Trace-Id": tid})
    time.sleep(0.2)  # let background tasks try to append their segments

    r = c.get("/traces/replay-000000002")
    assert r.status_code == 200
    data = r.json()
    assert data["segment_count"] == 2  # capped, never grows past the limit
    assert data["truncated"] is True
    assert data["limits"]["max_segments_per_trace"] == 2


def test_replay_exposes_error_source(client):
    c, _ = client
    c.get("/crash", headers={"X-Trace-Id": "replay-000000003"})
    r = c.get("/traces/replay-000000003")
    assert r.status_code == 200
    data = r.json()
    assert data["errors"] == [
        {"kind": "uncaught", "service": "upstream", "name": "GET /crash"}
    ]
