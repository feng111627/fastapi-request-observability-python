"""异常分类：未捕获、业务、参数校验、超时四类可区分，且日志与指标一致。"""
from tests.conftest import read_log_lines


def test_uncaught_exception_classified(client, store):
    """异常分类：未捕获异常归为 uncaught，响应与日志均带 trace_id。"""
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error_class"] == "uncaught"
    assert body["trace_id"]
    rec = [x for x in read_log_lines(store) if x.get("path") == "/boom"]
    assert rec and rec[0]["error_class"] == "uncaught"
    assert rec[0]["trace_id"] == body["trace_id"]


def test_business_error_classified(client, store):
    """异常分类：业务异常归为 business，保留业务错误码与状态码。"""
    r = client.get("/biz", params={"fail": "true"})
    assert r.status_code == 409
    assert r.json()["error_class"] == "business"
    assert r.json()["code"] == "BIZ_NOT_ALLOWED"
    rec = [x for x in read_log_lines(store) if x.get("path") == "/biz"]
    assert rec[0]["error_class"] == "business" and rec[0]["status_code"] == 409


def test_validation_error_classified(client, store):
    """异常分类：参数校验失败归为 validation，状态码 422。"""
    r = client.get("/items/not-an-int", params={"q": "abc"})
    assert r.status_code == 422
    assert r.json()["error_class"] == "validation"
    r2 = client.get("/items/1", params={"q": "ab"})  # q 太短
    assert r2.status_code == 422
    recs = [x for x in read_log_lines(store) if x.get("error_class") == "validation"]
    assert len(recs) == 2


def test_timeout_classified(client, store):
    """异常分类：超时归为 timeout，状态码 504。"""
    r = client.get("/timeout")
    assert r.status_code == 504
    assert r.json()["error_class"] == "timeout"
    rec = [x for x in read_log_lines(store) if x.get("path") == "/timeout"]
    assert rec[0]["error_class"] == "timeout"


def test_metrics_agree_with_logs(client, store, app):
    """指标一致性：同一请求在日志与指标中的分类、慢标记结论一致。"""
    client.get("/")
    client.get("/boom")
    client.get("/biz", params={"fail": "true"})
    client.get("/items/1", params={"q": "abc"})

    logs = [x for x in read_log_lines(store) if "status_code" in x]
    snap = app.state.metrics.snapshot()
    assert snap["total"] == len(logs)
    assert snap["errors"] == sum(1 for x in logs if x["error_class"] != "none")
    # 每个日志记录都能在指标维度中找到对应桶
    buckets = {
        (r["method"], r["path"], r["status_code"], r["error_class"], r["is_slow"])
        for r in snap["requests"]
    }
    for rec in logs:
        key = (
            rec["method"], rec["path"], rec["status_code"],
            rec["error_class"], rec["is_slow"],
        )
        assert key in buckets
