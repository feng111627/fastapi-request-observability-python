"""链路标识：并发隔离、客户端标识校验、后台任务与对外调用传播。"""
import asyncio
import json

import httpx2 as httpx

from tests.conftest import read_log_lines


def test_server_generates_trace_id_when_absent(client):
    """链路标识：无标识请求由服务端生成，并写入响应头与响应体。"""
    r = client.get("/")
    assert r.status_code == 200
    tid = r.headers["x-request-id"]
    assert tid and r.json()["trace_id"] == tid


def test_client_supplied_id_accepted_when_valid(client):
    """链路标识：合法客户端标识被采纳并原样返回。"""
    r = client.get("/", headers={"X-Request-ID": "client-abc-123"})
    assert r.json()["trace_id"] == "client-abc-123"
    assert r.headers["x-request-id"] == "client-abc-123"


def test_client_supplied_id_rejected_when_invalid(client):
    """链路标识：非法客户端标识（过短/含特殊字符）被丢弃并重新生成。"""
    for bad in ["x", "a" * 100, "bad id!", "../../etc"]:
        r = client.get("/", headers={"X-Request-ID": bad})
        assert r.json()["trace_id"] != bad


def test_concurrent_requests_isolated(app, store):
    """并发隔离：50 个并发请求各自携带不同标识，响应与日志互不串号。"""

    async def main():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            async def one(i):
                tid = f"req-{i:08d}"
                r = await c.get("/slow", params={"ms": 20},
                                headers={"X-Request-ID": tid})
                return tid, r

            return await asyncio.gather(*[one(i) for i in range(50)])

    results = asyncio.run(main())
    for tid, r in results:
        assert r.json()["trace_id"] == tid
        assert r.headers["x-request-id"] == tid

    logged = {rec["trace_id"] for rec in read_log_lines(store)}
    assert logged == {tid for tid, _ in results}


def test_client_id_does_not_leak_to_other_requests(client):
    """并发隔离：客户端传入的标识不会污染后续无标识请求。"""
    client.get("/", headers={"X-Request-ID": "client-zzz-001"})
    r = client.get("/")
    assert r.json()["trace_id"] != "client-zzz-001"


def test_background_task_inherits_trace_id(client, store):
    """后台任务：任务日志中的 trace_id 与触发请求一致。"""
    r = client.get("/bg")
    tid = r.json()["trace_id"]
    events = [
        rec for rec in read_log_lines(store)
        if rec.get("event") == "background_task"
    ]
    assert events and all(e["trace_id"] == tid for e in events)


def test_outbound_call_propagates_trace_id(client):
    """对外调用：出站请求头携带当前 trace_id，下游收到同一标识。"""
    r = client.get("/outbound")
    body = r.json()
    assert body["trace_id"]
    assert body["downstream_trace_id"] == body["trace_id"]
