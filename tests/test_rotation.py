"""Concurrent writes through the rotating handler stay intact."""
import json
import os
import threading

from app.logging_setup import get_request_logger


def _stress(log_dir, name, max_bytes, backup_count, workers=8, rows=200):
    logger = get_request_logger(str(log_dir), name, max_bytes, backup_count)

    def write(i):
        for j in range(rows):
            logger.info("row", extra={"trace": {"worker": i, "seq": j}})

    threads = [threading.Thread(target=write, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return logger


def test_no_loss_without_rotation(tmp_path):
    _stress(tmp_path, "full.log", max_bytes=10**9, backup_count=1)
    with open(tmp_path / "full.log", encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh]
    assert len(recs) == 1600
    assert {(r["worker"], r["seq"]) for r in recs} == {
        (i, j) for i in range(8) for j in range(200)
    }


def test_rotation_never_interleaves_lines(tmp_path):
    _stress(tmp_path, "rot.log", max_bytes=4096, backup_count=3)
    files = [f for f in os.listdir(tmp_path) if f.startswith("rot.log")]
    assert len(files) > 1  # rotation happened
    for f in files:
        with open(tmp_path / f, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)  # raises on interleaved/corrupt line
                assert "worker" in rec and "seq" in rec
