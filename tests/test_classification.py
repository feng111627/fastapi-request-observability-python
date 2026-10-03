"""Exception taxonomy: business / validation / timeout / uncaught."""
from app.metrics import metrics_registry
from tests.conftest import read_logs


def test_business_error_classified(client):
    c, log_dir = client
    r = c.post("/orders", json={"item": "a", "quantity": 99})
    assert r.status_code == 409
    log = read_logs(log_dir)[-1]
    assert log["error_kind"] == "business"
    assert log["status"] == 409
    assert metrics_registry.snapshot().errors["business"] == 1


def test_validation_error_classified(client):
    c, log_dir = client
    r = c.post("/orders", json={"item": "a"})  # missing quantity
    assert r.status_code == 422
    log = read_logs(log_dir)[-1]
    assert log["error_kind"] == "validation"
    assert metrics_registry.snapshot().errors["validation"] == 1


def test_timeout_error_classified(client):
    c, log_dir = client
    r = c.get("/timeout")
    assert r.status_code == 504
    log = read_logs(log_dir)[-1]
    assert log["error_kind"] == "timeout"
    assert metrics_registry.snapshot().errors["timeout"] == 1


def test_uncaught_error_classified(client):
    c, log_dir = client
    r = c.get("/crash")
    assert r.status_code == 500
    body = r.json()
    assert body["error_kind"] == "uncaught"
    assert body["trace_id"] == r.headers["x-trace-id"]
    log = read_logs(log_dir)[-1]
    assert log["error_kind"] == "uncaught"
    assert metrics_registry.snapshot().errors["uncaught"] == 1


def test_metrics_and_logs_agree(client):
    c, log_dir = client
    c.get("/")
    c.post("/orders", json={"item": "a", "quantity": 99})
    c.get("/crash")
    snap = metrics_registry.snapshot()
    logs = read_logs(log_dir)
    assert snap.total == len(logs) == 3
    logged_kinds = sorted(l["error_kind"] for l in logs if l["error_kind"] != "none")
    metric_kinds = sorted(k for k, n in snap.errors.items() for _ in range(n))
    assert logged_kinds == metric_kinds
