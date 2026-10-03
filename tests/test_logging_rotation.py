"""日志轮转：并发写入不丢内容、不交错，轮转后每行仍是完整 JSON。"""
import json
import os
import threading

from observability import setup_logging, get_logger


def _read_all_lines(directory):
    lines = []
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), encoding="utf-8") as f:
            lines.extend(ln for ln in f if ln.strip())
    return lines


def test_concurrent_writes_no_loss_no_interleave(tmp_path):
    """日志轮转：8 线程并发写 400 条，容量足够时一条不丢、每行可解析。"""
    setup_logging(str(tmp_path / "app.log"), max_bytes=1024 * 1024, backup_count=3)
    log = get_logger()

    def worker(n):
        for i in range(50):
            log.info({"trace_id": f"t{n:02d}", "seq": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _read_all_lines(tmp_path)
    assert len(lines) == 400
    seen = set()
    for ln in lines:
        rec = json.loads(ln)  # 每行都是完整 JSON，证明无交错
        seen.add((rec["trace_id"], rec["seq"]))
    assert len(seen) == 400  # 无重复、无丢失


def test_rotation_keeps_files_parseable(tmp_path):
    """日志轮转：小容量触发轮转后，所有保留文件每行仍是完整 JSON。"""
    setup_logging(str(tmp_path / "app.log"), max_bytes=2048, backup_count=3)
    log = get_logger()
    for i in range(400):
        log.info({"trace_id": "rotate-me", "seq": i})

    files = os.listdir(tmp_path)
    assert len(files) > 1  # 确实发生了轮转
    for ln in _read_all_lines(tmp_path):
        rec = json.loads(ln)
        assert rec["trace_id"] == "rotate-me"


def test_concurrent_writes_during_rotation(tmp_path):
    """日志轮转：轮转进行中并发写入，保留文件内无半行/交错内容。"""
    setup_logging(str(tmp_path / "app.log"), max_bytes=4096, backup_count=10)
    log = get_logger()

    def worker(n):
        for i in range(100):
            log.info({"trace_id": f"rot-{n}", "seq": i, "pad": "x" * 32})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for ln in _read_all_lines(tmp_path):
        json.loads(ln)  # 任意一行损坏都会抛异常
