"""慢请求：可配置阈值、分阶段耗时、运行中调整阈值。"""
from tests.conftest import read_log_lines


def test_slow_request_flagged_over_threshold(client, store):
    """慢请求：耗时超过阈值的请求被标记 is_slow 且记录分阶段耗时。"""
    store.update(slow_threshold_ms=50.0)
    r = client.get("/slow", params={"ms": 120})
    assert r.status_code == 200
    rec = [x for x in read_log_lines(store) if x.get("path") == "/slow"][0]
    assert rec["is_slow"] is True
    assert rec["duration_ms"] >= 100
    stages = rec["stage_ms"]
    assert set(stages) == {"receive_ms", "app_ms", "send_ms"}
    assert all(v >= 0 for v in stages.values())
    # 分阶段之和与总耗时的误差在合理范围内（计时舍入）
    assert abs(sum(stages.values()) - rec["duration_ms"]) < 1.0


def test_fast_request_not_flagged(client, store):
    """慢请求：阈值以下的请求不被标记。"""
    store.update(slow_threshold_ms=500.0)
    client.get("/slow", params={"ms": 5})
    rec = [x for x in read_log_lines(store) if x.get("path") == "/slow"][0]
    assert rec["is_slow"] is False


def test_threshold_change_takes_effect_at_runtime(client, store):
    """慢请求：运行中调低阈值后，新请求按新阈值判定。"""
    store.update(slow_threshold_ms=1000.0)
    client.get("/slow", params={"ms": 100})
    rec1 = [x for x in read_log_lines(store) if x.get("path") == "/slow"][0]
    assert rec1["is_slow"] is False

    store.update(slow_threshold_ms=50.0)
    client.get("/slow", params={"ms": 100})
    recs = [x for x in read_log_lines(store) if x.get("path") == "/slow"]
    assert recs[1]["is_slow"] is True
    assert recs[1]["slow_threshold_ms"] == 50.0


def test_slow_flag_consistent_between_log_and_metrics(client, store, app):
    """慢请求：日志与指标对同一请求的 is_slow 结论一致。"""
    store.update(slow_threshold_ms=50.0)
    client.get("/slow", params={"ms": 120})
    client.get("/slow", params={"ms": 5})
    logs = [x for x in read_log_lines(store) if x.get("path") == "/slow"]
    snap = app.state.metrics.snapshot()
    slow_in_metrics = sum(
        r["count"] for r in snap["requests"] if r["path"] == "/slow" and r["is_slow"]
    )
    assert slow_in_metrics == sum(1 for x in logs if x["is_slow"]) == 1
