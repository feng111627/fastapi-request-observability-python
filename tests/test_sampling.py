"""采样：边界值 0/1、错误与慢请求必保留、确定性、运行中变更可预期。"""
from observability import ConfigStore, ObservabilityConfig, Sampler
from tests.conftest import read_log_lines


def test_rate_zero_drops_normal_but_keeps_errors_and_slow(client, store):
    """采样边界：rate=0 时普通请求被采掉，错误与慢请求链路仍完整保留。"""
    store.update(sample_rate=0.0, slow_threshold_ms=50.0)
    client.get("/")                                  # 普通 → 采掉
    client.get("/boom")                              # 错误 → 保留
    client.get("/biz", params={"fail": "true"})      # 业务错误 → 保留
    client.get("/items/1", params={"q": "x"})        # 校验失败 → 保留
    client.get("/timeout")                           # 超时 → 保留
    client.get("/slow", params={"ms": 120})          # 慢 → 保留

    recs = read_log_lines(store)
    paths = sorted(x["path"] for x in recs)
    assert paths == ["/biz", "/boom", "/items/1", "/slow", "/timeout"]
    assert all(x["error_class"] != "none" or x["is_slow"] for x in recs)


def test_rate_one_keeps_everything(client, store):
    """采样边界：rate=1 时所有请求都保留。"""
    store.update(sample_rate=1.0)
    for _ in range(5):
        client.get("/")
    assert len(read_log_lines(store)) == 5


def test_metrics_unaffected_by_sampling(client, store, app):
    """采样边界：采样只影响日志，指标始终全量记录。"""
    store.update(sample_rate=0.0)
    for _ in range(3):
        client.get("/")
    assert read_log_lines(store) == []
    assert app.state.metrics.snapshot()["total"] == 3


def test_sampling_decision_is_deterministic():
    """采样边界：同一 trace_id 在同一采样率下决策稳定（哈希采样）。"""
    s = Sampler(ConfigStore(ObservabilityConfig(sample_rate=0.5)))
    for tid in ["a" * 8, "b" * 16, "c" * 32]:
        d1 = s.should_keep(tid, is_error=False, is_slow=False)
        d2 = s.should_keep(tid, is_error=False, is_slow=False)
        assert d1 == d2


def test_partial_rate_keeps_roughly_proportion():
    """采样边界：rate=0.5 时保留比例在统计容差内。"""
    s = Sampler(ConfigStore(ObservabilityConfig(sample_rate=0.5)))
    kept = sum(
        s.should_keep(f"trace-{i:032d}", is_error=False, is_slow=False)
        for i in range(2000)
    )
    assert 850 < kept < 1150  # 期望约 1000


def test_rate_change_at_runtime_is_predictable(client, store):
    """采样变更：运行中改采样率只影响变更后的请求，行为可预期。"""
    store.update(sample_rate=1.0)
    client.get("/")
    assert len(read_log_lines(store)) == 1

    store.update(sample_rate=0.0)
    client.get("/")
    client.get("/")
    assert len(read_log_lines(store)) == 1  # 变更后的普通请求被采掉

    store.update(sample_rate=1.0)
    client.get("/")
    assert len(read_log_lines(store)) == 2
