"""Sampling boundaries: errors/slow always kept, runtime rate changes."""
from app.config import settings_holder
from app.metrics import metrics_registry
from app.sampling import sampling_policy
from tests.conftest import read_logs


def test_rate_zero_drops_normal_but_keeps_errors_and_slow(client):
    c, log_dir = client
    sampling_policy.set_rate(0.0)
    settings_holder.update(slow_request_ms=100.0)

    for _ in range(5):
        c.get("/")                                   # normal -> dropped
    c.get("/slow", params={"ms": 150})               # slow -> kept
    c.post("/orders", json={"item": "a", "quantity": 99})  # error -> kept
    c.get("/crash")                                  # uncaught -> kept

    logs = read_logs(log_dir)
    kinds = sorted(l["error_kind"] for l in logs)
    assert kinds == ["business", "none", "uncaught"]
    slow_logs = [l for l in logs if l["error_kind"] == "none"]
    assert slow_logs[0]["slow"] is True

    snap = metrics_registry.snapshot()
    assert snap.total == 8            # metrics count every request
    assert snap.sampled_out == 5      # only the 5 normal ones dropped


def test_rate_one_keeps_everything(client):
    c, log_dir = client
    sampling_policy.set_rate(1.0)
    for _ in range(5):
        c.get("/")
    assert len(read_logs(log_dir)) == 5
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
    assert len(read_logs(log_dir)) == 1
    sampling_policy.set_rate(0.0)
    c.get("/")
    c.get("/")
    assert len(read_logs(log_dir)) == 1  # change applies to later requests only
    sampling_policy.set_rate(1.0)
    c.get("/")
    assert len(read_logs(log_dir)) == 2
