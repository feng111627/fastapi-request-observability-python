"""Background tasks survive the response, results and failures are visible."""
import time

from tests.conftest import read_logs


def test_background_task_result_is_queryable_after_response(client):
    c, log_dir = client
    r = c.get("/background", headers={"X-Trace-Id": "bg-result-00001"})
    assert r.status_code == 200
    assert r.json()["scheduled"] is True
    # task keeps running after the response; wait briefly then query by id
    deadline = time.time() + 2
    while time.time() < deadline:
        got = c.get("/tasks/bg-result-00001").json()
        if got["count"]:
            break
        time.sleep(0.01)
    assert got["found"] is True
    result = got["results"][0]
    assert result["status"] == "success"
    assert result["trace_id"] == "bg-result-00001"
    assert "bg-result-00001" in result["result"]  # result attributable to request
    assert result["duration_ms"] >= 15  # the task really did its ~20ms sleep


def test_failed_background_task_is_classified_not_swallowed(client):
    c, log_dir = client
    c.get("/background", params={"fail": True}, headers={"X-Trace-Id": "bg-fail-0000001"})
    deadline = time.time() + 2
    while time.time() < deadline:
        got = c.get("/tasks/bg-fail-0000001").json()
        if got["count"]:
            break
        time.sleep(0.01)
    result = got["results"][0]
    assert result["status"] == "failed"
    assert result["error_kind"] == "uncaught"
    assert "background task failed on purpose" in result["error"]

    metrics = c.get("/metrics").json()
    assert metrics["background_total"] == 1
    assert metrics["background_failed"] == 1
    assert metrics["errors"].get("uncaught") == 1

    # the failure is also a queryable log segment under the same id
    logs = read_logs(log_dir)
    task_logs = [l for l in logs if l.get("msg") == "background_task"
                 and l["trace_id"] == "bg-fail-0000001"]
    assert len(task_logs) == 1
    assert task_logs[0]["status"] == "failed"


def test_background_task_appears_in_trace_replay(client):
    c, log_dir = client
    c.get("/background", headers={"X-Trace-Id": "bg-replay-00001"})
    deadline = time.time() + 2
    rep = {}
    while time.time() < deadline:
        rep = c.get("/replay/bg-replay-00001").json()
        kinds = [s["kind"] for s in rep.get("timeline", [])]
        if "background" in kinds:
            break
        time.sleep(0.01)
    assert rep["found"] is True
    kinds = [s["kind"] for s in rep["timeline"]]
    assert kinds == ["entry", "background"]
    assert rep["summary"]["background_tasks"] == 1
    assert rep["summary"]["background_failed"] == 0


def test_concurrent_background_tasks_keep_their_own_trace(client):
    c, log_dir = client
    n = 15
    for i in range(n):
        c.get("/background", headers={"X-Trace-Id": f"bg-conc-{i:04d}-abcd"})
    # every task lands under exactly its own request's id
    deadline = time.time() + 3
    while time.time() < deadline:
        done = sum(
            c.get(f"/tasks/bg-conc-{i:04d}-abcd").json()["count"] for i in range(n)
        )
        if done == n:
            break
        time.sleep(0.02)
    assert done == n
    for i in range(n):
        tid = f"bg-conc-{i:04d}-abcd"
        got = c.get(f"/tasks/{tid}").json()
        assert got["count"] == 1
        assert got["results"][0]["trace_id"] == tid
        assert tid in got["results"][0]["result"]
