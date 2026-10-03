"""Slow-request flagging and per-stage timings."""
from app.config import settings_holder
from app.metrics import metrics_registry
from tests.conftest import read_logs


def test_slow_request_flagged_with_stages(client):
    c, log_dir = client
    settings_holder.update(slow_request_ms=100.0)
    c.get("/slow", params={"ms": 150})
    log = read_logs(log_dir)[-1]
    assert log["slow"] is True
    assert log["slow_threshold_ms"] == 100.0
    stage_names = [s["name"] for s in log["stages"]]
    assert "heavy-compute" in stage_names
    stage = next(s for s in log["stages"] if s["name"] == "heavy-compute")
    assert stage["duration_ms"] >= 140
    assert metrics_registry.snapshot().slow == 1


def test_fast_request_not_flagged(client):
    c, log_dir = client
    settings_holder.update(slow_request_ms=1000.0)
    c.get("/")
    log = read_logs(log_dir)[-1]
    assert log["slow"] is False
    assert metrics_registry.snapshot().slow == 0


def test_threshold_boundary_is_slow(client):
    c, log_dir = client
    settings_holder.update(slow_request_ms=0.0)  # everything is >= 0
    c.get("/")
    assert read_logs(log_dir)[-1]["slow"] is True


def test_threshold_change_takes_effect_at_runtime(client):
    c, log_dir = client
    settings_holder.update(slow_request_ms=10_000.0)
    c.get("/slow", params={"ms": 120})
    assert read_logs(log_dir)[-1]["slow"] is False
    settings_holder.update(slow_request_ms=100.0)
    c.get("/slow", params={"ms": 120})
    assert read_logs(log_dir)[-1]["slow"] is True
