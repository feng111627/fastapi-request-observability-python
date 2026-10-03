import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import settings_holder
from app.metrics import metrics_registry
from app.sampling import sampling_policy
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
    )
    sampling_policy.set_rate(1.0)
    metrics_registry.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, tmp_path


def read_logs(log_dir):
    path = os.path.join(str(log_dir), "requests.log")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]
