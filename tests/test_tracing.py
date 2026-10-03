"""Trace id propagation, concurrent isolation, client-id safety."""
import threading

from fastapi.testclient import TestClient

from main import app
from tests.conftest import read_logs


def test_server_generates_trace_id_and_echoes_header(client):
    c, log_dir = client
    r = c.get("/")
    tid = r.headers["x-trace-id"]
    assert r.json()["trace_id"] == tid
    logs = read_logs(log_dir)
    assert logs[0]["trace_id"] == tid


def test_valid_client_trace_id_is_propagated(client):
    c, log_dir = client
    r = c.get("/", headers={"X-Trace-Id": "client-trace-0001"})
    assert r.headers["x-trace-id"] == "client-trace-0001"
    assert r.json()["trace_id"] == "client-trace-0001"


def test_malformed_client_trace_id_is_replaced(client):
    c, log_dir = client
    r = c.get("/", headers={"X-Trace-Id": "bad id with spaces!"})
    tid = r.headers["x-trace-id"]
    assert tid != "bad id with spaces!"
    assert len(tid) == 32


def test_concurrent_requests_do_not_cross_trace_ids(client):
    c, log_dir = client
    errors = []

    def hit(i):
        try:
            # one client per thread: TestClient is not thread-safe
            with TestClient(app, raise_server_exceptions=False) as tc:
                tid = f"req-{i:04d}-abcdefgh"
                r = tc.get("/", headers={"X-Trace-Id": tid})
                assert r.headers["x-trace-id"] == tid
                assert r.json()["trace_id"] == tid
        except AssertionError as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    logs = read_logs(log_dir)
    logged_ids = {l["trace_id"] for l in logs if l["path"] == "/"}
    assert logged_ids == {f"req-{i:04d}-abcdefgh" for i in range(30)}


def test_background_task_and_outbound_headers_share_trace(client):
    c, log_dir = client
    r = c.get("/outbound", headers={"X-Trace-Id": "outbound-000001"})
    assert r.json()["would_send_headers"] == {"X-Trace-Id": "outbound-000001"}
    r = c.get("/background", headers={"X-Trace-Id": "bg-000000000001"})
    assert r.json()["trace_id"] == "bg-000000000001"
