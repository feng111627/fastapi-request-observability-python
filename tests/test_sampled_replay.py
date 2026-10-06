"""Sampling must not erase real traces.

Business coverage:

* normal requests whose detail is sampled out still leave an entry
  anchor, so the entry + its downstream calls + its background tasks can
  be fully replayed by trace id (rate 0 is the strongest case);
* cross-service traces stay joinable under sampling: the downstream
  service's own segment under the same id is found;
* the not-found ``reason`` is honest — a never-seen id is ``unknown``
  even right after sampled-out traffic, and only a line that really
  existed and was deleted by rotation is ``rotated_away``;
* concurrent requests under sampling never swap segments between ids.
"""
import concurrent.futures
import os
import time

from app.logging_setup import reset_loggers
from app.sampling import sampling_policy
from tests.conftest import read_logs


def _wait_for_background(c, tid, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    rep = {}
    while time.time() < deadline:
        rep = c.get(f"/replay/{tid}").json()
        if any(s["kind"] == "background" for s in rep.get("timeline", ())):
            return rep
        time.sleep(0.01)
    return rep


def test_sampled_out_normal_request_is_still_fully_replayable(client):
    """Entry anchor + downstream + background all found at rate 0."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)  # every normal request loses detail...

    tid = "sample-replay-000001"
    chain = c.get("/chain", headers={"X-Trace-Id": tid})
    assert chain.status_code == 200
    bg = c.get("/background", headers={"X-Trace-Id": tid})
    assert bg.status_code == 200

    rep = _wait_for_background(c, tid)
    assert rep["found"] is True, "sampled-out trace must still replay"
    kinds = [s["kind"] for s in rep["timeline"]]
    assert "entry" in kinds, "the entry request anchor must exist"
    assert "downstream" in kinds, "the downstream call segment must exist"
    assert "background" in kinds, "the background task segment must exist"

    # The entry is the minimal anchor: present, marked sampled=false.
    entries = [s for s in rep["timeline"] if s["kind"] == "entry"]
    upstream_entries = [s for s in entries if s.get("service") == "upstream"]
    assert upstream_entries and upstream_entries[0]["sampled"] is False
    # Replay-essential conclusions survive even without full detail.
    assert upstream_entries[0]["status"] == 200
    assert upstream_entries[0]["duration_ms"] is not None

    # The detailed log line really was skipped, only an anchor written.
    raw = [l for l in read_logs(log_dir) if l.get("trace_id") == tid and l.get("msg") == "request"]
    assert all(l.get("sampled") is False for l in raw if l.get("service") == "upstream")


def test_cross_service_segments_found_under_sampling(client):
    """Downstream service's own entry under the same id is still found."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tid = "sample-cross-0000001"
    resp = c.get("/chain", headers={"X-Trace-Id": tid})
    assert resp.status_code == 200
    assert resp.json()["matched"] is True

    rep = c.get(f"/replay/{tid}").json()
    assert rep["found"] is True
    timeline = rep["timeline"]
    services = {s.get("service") for s in timeline}
    assert "downstream" in services, "downstream segment must join the same trace"
    # downstream service's own entry (it processed under our id)...
    downstream_entries = [
        s for s in timeline
        if s["kind"] == "entry" and s.get("service") == "downstream"
    ]
    assert downstream_entries, "downstream must have logged its own entry"
    # ...plus the upstream-observed downstream_call segment.
    calls = [s for s in timeline if s["kind"] == "downstream"]
    assert calls and calls[0]["matched"] is True
    assert calls[0]["trace_sent"] == tid == calls[0]["trace_received"]
    assert rep["summary"]["downstream_calls"] == 1
    assert rep["summary"]["downstream_mismatches"] == 0


def test_never_seen_id_is_unknown_never_rotated_away(client):
    """Sampled-out traffic must not make unknown ids look rotated away."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    for i in range(10):
        r = c.get("/", headers={"X-Trace-Id": f"sample-busy-{i:08d}"})
        assert r.status_code == 200

    # The log file is right there and nothing rotated, yet this id never
    # existed: it must be reported as unknown, not rotated_away.
    assert os.path.exists(os.path.join(str(log_dir), "requests.log"))
    rep = c.get("/replay/totally-made-up-id-9999").json()
    assert rep["found"] is False
    assert rep["reason"] == "unknown"


def test_really_rotated_line_is_still_reported_rotated_away(client):
    """A line that existed and was deleted by rotation is rotated_away."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tid = "sample-rotate-gone-01"
    c.get("/", headers={"X-Trace-Id": tid})
    assert c.get(f"/replay/{tid}").json()["found"] is True  # anchor present

    from app.config import settings_holder
    settings_holder.update(rotation_max_bytes=600, rotation_backup_count=1)
    reset_loggers()
    present = True
    for i in range(200):
        c.get("/", headers={"X-Trace-Id": f"flood-{i:06d}-bbbbbb"})
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


def test_concurrent_sampled_requests_keep_segments_under_own_id(client):
    """20 concurrent rate-0 traces never cross-assign segments."""
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    tids = [f"sample-conc-{i:08d}" for i in range(20)]

    def hit(tid: str) -> str:
        r = c.get("/chain", headers={"X-Trace-Id": tid})
        assert r.status_code == 200
        return tid

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(hit, tids))

    for tid in tids:
        rep = c.get(f"/replay/{tid}").json()
        assert rep["found"] is True, tid
        timeline = rep["timeline"]
        # Every segment belongs to this id and this id only.
        ids_on_segments = set()
        raw = [l for l in read_logs(log_dir)]
        for seg in timeline:
            assert seg["kind"] in {"entry", "downstream", "background"}
        # No segment from another trace leaked in: timeline check plus
        # the log's trace_sent/trace_received must be this id.
        for seg in timeline:
            if seg["kind"] == "downstream":
                assert seg["trace_sent"] == tid
                assert seg["trace_received"] == tid
        own = [l for l in raw if l.get("trace_id") == tid]
        assert any(l.get("msg") == "request" for l in own)
        calls = [l for l in own if l.get("msg") == "downstream_call"]
        assert len(calls) == 1
        assert all(l.get("trace_sent") == tid and l.get("trace_received") == tid
                   for l in calls)

    # And the unrelated id is still unknown, not blamed on rotation.
    rep = c.get("/replay/sample-conc-unknown-zz").json()
    assert rep["reason"] == "unknown"
