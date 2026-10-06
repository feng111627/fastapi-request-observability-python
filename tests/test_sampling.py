"""Sampling boundaries: errors/slow always kept, runtime rate changes.

Sampling trims the *detail* of normal requests (stage timings etc.), not
the trace's existence: every request keeps a minimal ``sampled=false``
anchor entry so its chain can still be replayed. Fully sampled entries
carry ``sampled=true``. The metrics accounting (``sampled_out`` counts
rate-dropped normal requests; errors/slow are never dropped) is the
statistical contract and is asserted here.
"""
from app.config import settings_holder
from app.metrics import metrics_registry
from app.sampling import sampling_policy
from tests.conftest import read_logs


def full_entries(logs):
    """Entry records written with full detail (kept by sampling)."""
    return [l for l in logs if l.get("msg") == "request" and l.get("sampled", True) is True]


def anchors(logs):
    """Minimal anchor entries left by sampled-out requests."""
    return [l for l in logs if l.get("msg") == "request" and l.get("sampled") is False]


def test_rate_zero_drops_normal_but_keeps_errors_and_slow(client):
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    settings_holder.update(slow_request_ms=100.0)

    normal_ids = [f"sample-drop-{i:08d}" for i in range(5)]
    for tid in normal_ids:
        c.get("/", headers={"X-Trace-Id": tid})      # normal -> detail dropped
    c.get("/slow", params={"ms": 150})               # slow -> kept
    c.post("/orders", json={"item": "a", "quantity": 99})  # error -> kept
    c.get("/crash")                                  # uncaught -> kept

    logs = read_logs(log_dir)
    kept = full_entries(logs)
    dropped = anchors(logs)

    # Statistical contract: exactly the 5 normal requests were sampled
    # out; errors and slow requests always keep their full record.
    kinds = sorted(l["error_kind"] for l in kept)
    assert kinds == ["business", "none", "uncaught"]
    slow_kept = [l for l in kept if l["error_kind"] == "none"]
    assert slow_kept[0]["slow"] is True
    assert len(dropped) == 5
    assert all(l["slow"] is False and l["error_kind"] == "none" for l in dropped)
    # Dropped detail: anchors carry no stage timings.
    assert all("stages" not in l for l in dropped)
    # But the anchor is still a real entry with replay-essential fields.
    anchor_ids = {l["trace_id"] for l in dropped}
    assert anchor_ids == set(normal_ids)
    assert all(l["status"] == 200 and "duration_ms" in l for l in dropped)

    snap = metrics_registry.snapshot()
    assert snap.total == 8            # metrics count every request
    assert snap.sampled_out == 5      # only the 5 normal ones dropped


def test_rate_one_keeps_everything(client):
    c, log_dir = client
    sampling_policy.set_rate(1.0)
    for _ in range(5):
        c.get("/")
    logs = read_logs(log_dir)
    assert len(full_entries(logs)) == 5
    assert anchors(logs) == []
    assert metrics_registry.snapshot().sampled_out == 0


def test_invalid_rate_rejected():
    import pytest

    with pytest.raises(ValueError):
        sampling_policy.set_rate(-0.1)
    with pytest.raises(ValueError):
        sampling_policy.set_rate(1.1)


def test_runtime_rate_change_is_predictable(client):
    c, log_dir = client
    sampling_policy.set_rate(1.0)
    c.get("/")
    assert len(full_entries(read_logs(log_dir))) == 1
    sampling_policy.set_rate(0.0)
    c.get("/", headers={"X-Trace-Id": "rate-switch-00001"})
    c.get("/", headers={"X-Trace-Id": "rate-switch-00002"})
    logs = read_logs(log_dir)
    # change applies to later requests only: still one full record...
    assert len(full_entries(logs)) == 1
    # ...but the two later requests leave anchors and remain replayable.
    assert {l["trace_id"] for l in anchors(logs)} == {
        "rate-switch-00001",
        "rate-switch-00002",
    }
    sampling_policy.set_rate(1.0)
    c.get("/")
    assert len(full_entries(read_logs(log_dir))) == 2
