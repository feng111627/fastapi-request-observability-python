"""Cross-service trace propagation: same id, verification, mismatch, isolation."""
import threading

from fastapi.testclient import TestClient

from main import app
from tests.conftest import read_logs


def test_downstream_receives_same_trace_id(client):
    c, log_dir = client
    r = c.get("/chain", headers={"X-Trace-Id": "cross-svc-00001"})
    assert r.status_code == 200
    body = r.json()
    assert body["trace_sent"] == "cross-svc-00001"
    assert body["trace_received"] == "cross-svc-00001"
    assert body["matched"] is True
    # downstream saw exactly what upstream sent (its own body confirms it)
    assert body["downstream_body"]["received_trace_id"] == "cross-svc-00001"
    assert body["downstream_body"]["header_trace_id"] == "cross-svc-00001"


def test_downstream_logs_are_queryable_under_same_id(client):
    c, log_dir = client
    c.get("/chain", headers={"X-Trace-Id": "cross-svc-00002"})
    logs = read_logs(log_dir)
    entry_services = {
        (l["msg"], l.get("service"))
        for l in logs
        if l["trace_id"] == "cross-svc-00002"
    }
    # both services logged the same trace id independently
    assert ("request", "upstream") in entry_services
    assert ("request", "downstream") in entry_services


def test_mismatch_is_exposed_not_swallowed(client):
    c, log_dir = client
    r = c.get("/chain", params={"lie": True}, headers={"X-Trace-Id": "cross-svc-00003"})
    assert r.status_code == 502
    body = r.json()
    assert body["error_kind"] == "propagation"
    assert body["trace_id"] == "cross-svc-00003"
    # the downstream call segment records both ids and matched=False
    logs = read_logs(log_dir)
    calls = [l for l in logs if l.get("msg") == "downstream_call"
             and l["trace_id"] == "cross-svc-00003"]
    assert len(calls) == 1
    assert calls[0]["trace_sent"] == "cross-svc-00003"
    assert calls[0]["trace_received"] != "cross-svc-00003"
    assert calls[0]["matched"] is False
    metrics = c.get("/metrics").json()
    assert metrics["trace_mismatches"] == 1
    assert metrics["errors"].get("propagation") == 1


def test_concurrent_chains_do_not_cross_trace_ids(client):
    c, log_dir = client
    errors = []

    def hit(i):
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                tid = f"chain-conc-{i:04d}-ab"
                r = tc.get("/chain", headers={"X-Trace-Id": tid})
                assert r.status_code == 200, r.text
                b = r.json()
                assert b["trace_sent"] == tid
                assert b["trace_received"] == tid
                assert b["matched"] is True
        except AssertionError as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    logs = read_logs(log_dir)
    calls = [l for l in logs if l.get("msg") == "downstream_call"]
    by_sent = {l["trace_sent"]: l for l in calls}
    expected = {f"chain-conc-{i:04d}-ab" for i in range(20)}
    assert set(by_sent) == expected
    # no call ever paired the wrong sent/received identifiers
    assert all(l["trace_sent"] == l["trace_received"] for l in calls)
    assert all(l["matched"] is True for l in calls)
