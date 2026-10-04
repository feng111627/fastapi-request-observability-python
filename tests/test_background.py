"""Background tasks: results visible after response, failures classified."""
import time


def _wait_for_background_segment(c, trace_id, timeout=5.0):
    """Poll replay until the background segment of this trace shows up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/traces/{trace_id}")
        if r.status_code == 200:
            segs = [s for s in r.json()["timeline"] if s["kind"] == "background"]
            if segs:
                return segs[0]
        time.sleep(0.02)
    raise AssertionError(f"no background segment for {trace_id} within {timeout}s")


def test_background_result_visible_under_same_trace(client):
    c, _ = client
    r = c.post("/jobs?ms=30", headers={"X-Trace-Id": "bg-job-000000001"})
    assert r.json() == {"scheduled": True, "trace_id": "bg-job-000000001"}

    seg = _wait_for_background_segment(c, "bg-job-000000001")
    assert seg["error_kind"] == "none"
    assert "job done after 30ms" in seg["detail"]["result"]
    assert seg["duration_ms"] >= 25  # really ran for ~30ms

    metrics = c.get("/metrics").json()
    assert metrics["background_total"] >= 1
    assert metrics["background_errors"] == 0


def test_background_failure_is_classified_not_swallowed(client):
    c, _ = client
    c.post("/jobs?ms=10&fail=true", headers={"X-Trace-Id": "bg-job-000000002"})

    seg = _wait_for_background_segment(c, "bg-job-000000002")
    assert seg["error_kind"] == "background"
    assert "RuntimeError" in seg["detail"]["error"]

    metrics = c.get("/metrics").json()
    assert metrics["background_errors"] >= 1


def test_task_survives_request_end(client):
    c, _ = client
    r = c.post("/jobs?ms=150", headers={"X-Trace-Id": "bg-job-000000003"})
    # response returns well before the 150ms job finishes
    assert r.elapsed.total_seconds() < 0.15

    seg = _wait_for_background_segment(c, "bg-job-000000003")
    assert seg["error_kind"] == "none"
    # the segment lands after the request's own segment started
    timeline = c.get("/traces/bg-job-000000003").json()["timeline"]
    request_seg = next(s for s in timeline if s["kind"] == "request")
    assert seg["started_at"] >= request_seg["started_at"]


def test_concurrent_jobs_keep_their_own_trace(client):
    c, _ = client
    ids = [f"bg-job-{i:09d}" for i in range(10)]
    for tid in ids:
        c.post("/jobs?ms=20", headers={"X-Trace-Id": tid})
    for tid in ids:
        seg = _wait_for_background_segment(c, tid)
        assert seg["error_kind"] == "none"
    metrics = c.get("/metrics").json()
    assert metrics["background_total"] >= 10
