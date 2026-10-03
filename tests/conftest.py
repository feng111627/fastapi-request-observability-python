import json

import pytest

from observability import ConfigStore, ObservabilityConfig
from main import create_app


@pytest.fixture()
def store(tmp_path):
    """每个用例独立的配置与日志文件，互不影响。"""
    return ConfigStore(
        ObservabilityConfig(log_file=str(tmp_path / "app.log"), sample_rate=1.0)
    )


@pytest.fixture()
def app(store):
    return create_app(store)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def read_log_lines(store) -> list[dict]:
    """读取并解析 JSON 请求日志（只取请求记录，不含 background 事件）。"""
    path = store.get().log_file
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


@pytest.fixture()
def log_lines(store):
    return lambda: read_log_lines(store)


# ---- 用例覆盖与结果汇总输出 ----

_results: list[tuple[str, str, str]] = []


def pytest_runtest_logreport(report):
    if report.when == "call":
        # nodeid 形如 tests/test_x.py::test_y，从 item 取 docstring 需另存
        _results.append((report.nodeid, _COVERAGE.get(report.nodeid, "(未标注覆盖点)"), report.outcome))


_COVERAGE: dict[str, str] = {}


def pytest_collection_modifyitems(items):
    for item in items:
        doc = (item.obj.__doc__ or "").strip().splitlines()
        _COVERAGE[item.nodeid] = doc[0] if doc else "(未标注覆盖点)"


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_sep("=", "业务覆盖与用例结果汇总")
    for nodeid, coverage, outcome in _results:
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}.get(
            outcome, outcome
        )
        terminalreporter.write_line(f"[{mark}] {nodeid}")
        terminalreporter.write_line(f"       覆盖: {coverage}")
