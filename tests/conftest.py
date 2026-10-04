import json
import os
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

import downstream
from app.config import settings_holder
from app.metrics import metrics_registry
from app.sampling import sampling_policy
from app.segments import segment_store
from main import app


@pytest.fixture()
def client(tmp_path):
    """TestClient with isolated log dir, full sampling and reset metrics."""
    settings_holder.update(
        slow_request_ms=200.0,
        log_dir=str(tmp_path),
        log_file="requests.log",
        rotation_max_bytes=10**9,
        rotation_backup_count=1,
        service_name="upstream",
        downstream_url="http://127.0.0.1:1",
        segment_max_traces=1000,
        segment_max_per_trace=100,
    )
    segment_store.configure(1000, 100)
    segment_store.reset()
    sampling_policy.set_rate(1.0)
    metrics_registry.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, tmp_path


@pytest.fixture()
def downstream_server(tmp_path):
    """Real downstream HTTP service on 127.0.0.1 with isolated log dir."""
    downstream.downstream_settings.update(
        log_dir=str(tmp_path),
        log_file="downstream.log",
        service_name="downstream",
        rotation_max_bytes=10**9,
        rotation_backup_count=1,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = downstream.run_server(port=port)
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "downstream server did not start"
    yield f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True


def read_logs(log_dir, name="requests.log"):
    path = os.path.join(str(log_dir), name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


@pytest.fixture()
def asgi_server():
    """Factory: run an arbitrary ASGI app on 127.0.0.1, yield its base URL."""
    servers = []

    def start(asgi_app):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "asgi server did not start"
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield start
    for server in servers:
        server.should_exit = True
