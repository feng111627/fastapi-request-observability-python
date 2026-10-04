"""Cross-service trace propagation: same id downstream, mismatch exposed."""
import json
import threading

from fastapi.testclient import TestClient

from app.config import settings_holder
from main import app
from tests.conftest import read_logs


def test_relay_propagates_same_trace_id(client, downstream_server):
    c, log_dir = client
    base_url, _ = downstream_server
    settings_holder.update(downstream_url=base_url)

    r = c.get("/relay", headers={"X-Trace-Id": "relay-0000000001"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent_trace_id"] == "relay-0000000001"
    assert body["downstream"]["trace_id"] == "relay-0000000001"
    assert body["echo_matches"] is True

    # downstream's own log records the same id
    downstream_logs = [l for l in read_logs(log_dir, "downstream.log")
                       if l["trace_id"] == "relay-0000000001"]
    assert len(downstream_logs) == 1
    assert downstream_logs[0]["path"] == "/work"


def test_relay_replay_contains_downstream_segment(client, downstream_server):
    c, _ = client
    base_url, _ = downstream_server
    settings_holder.update(downstream_url=base_url)

    c.get("/relay", headers={"X-Trace-Id": "relay-0000000002"})
    r = c.get("/traces/relay-0000000002")
    assert r.status_code == 200
    kinds = {(s["kind"], s["service"]) for s in r.json()["timeline"]}
    assert ("request", "upstream") in kinds
    assert ("downstream", "upstream") in kinds
    # same-process downstream also records its own server-side segment
    assert ("request", "downstream") in kinds


def test_concurrent_relays_do_not_cross_trace_ids(client, downstream_server):
    c, _ = client
    base_url, _ = downstream_server
    settings_holder.update(downstream_url=base_url)
    errors = []

    def hit(i):
        try:
            with TestClient(app, raise_server_exceptions=False) as tc:
                tid = f"relay-{i:04d}-abcdefgh"
                r = tc.get("/relay", headers={"X-Trace-Id": tid})
                assert r.status_code == 200
                body = r.json()
                assert body["sent_trace_id"] == tid
                assert body["downstream"]["trace_id"] == tid
                assert body["echo_matches"] is True
        except AssertionError as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_trace_id_mismatch_is_exposed(client, asgi_server):
    c, _ = client

    async def spoofing_downstream(scope, receive, send):
        assert scope["type"] == "http"
        body = json.dumps({"ok": True}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"),
            (b"x-trace-id", b"spoofed-id-000000"),
        ]})
        await send({"type": "http.response.body", "body": body})

    settings_holder.update(downstream_url=asgi_server(spoofing_downstream))
    r = c.get("/relay", headers={"X-Trace-Id": "relay-0000000003"})
    assert r.status_code == 502
    body = r.json()
    assert body["error_kind"] == "trace_mismatch"
    assert body["sent_trace_id"] == "relay-0000000003"
    assert body["echoed_trace_id"] == "spoofed-id-000000"

    # mismatch is classified in metrics and visible in replay
    errors = c.get("/metrics").json()["errors"]
    assert errors.get("trace_mismatch", 0) >= 1
    timeline = c.get("/traces/relay-0000000003").json()
    downstream_segs = [s for s in timeline["timeline"] if s["kind"] == "downstream"]
    assert downstream_segs[0]["error_kind"] == "trace_mismatch"


def test_unreachable_downstream_is_classified(client):
    c, _ = client
    # client fixture points downstream_url at a closed port by default
    r = c.get("/relay", headers={"X-Trace-Id": "relay-0000000004"})
    assert r.status_code == 502
    assert r.json()["error_kind"] == "downstream"
    errors = c.get("/metrics").json()["errors"]
    assert errors.get("downstream", 0) >= 1
