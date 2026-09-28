"""日志行为测试：request_id 前缀、audit 无空行、gzip 归档、gate 自转封顶。

注意：setup_logging 操作的是全局 logger（test_server 在 import 时已绑定），
本文件用完必须把 handler 指回 .tmp-logs-test（见 restore fixture）。
"""

import gzip
import importlib.util
import json
import logging
import logging.handlers
import os

import pytest

from cc_exec_guard.logging_setup import _gzip_namer, _gzip_rotator, setup_logging

SERVER_TMP_LOG = os.path.join(os.getcwd(), ".tmp-logs-test")


@pytest.fixture()
def log_dir(tmp_path):
    d = str(tmp_path / "logs")
    setup_logging({"log_dir": d})
    yield d
    setup_logging({"log_dir": SERVER_TMP_LOG})  # 恢复，避免污染 test_server


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_app_log_has_request_id_prefix(log_dir):
    app_logger = logging.getLogger("cc-exec-guard")
    app_logger.info("hello", extra={"request_id": "abc123"})
    app_logger.info("no-extra-here")
    content = _read(os.path.join(log_dir, "app.log"))
    assert "[abc123]" in content
    assert "[-]" in content  # 无请求上下文默认记 -


def test_audit_no_blank_lines(log_dir):
    audit_logger = logging.getLogger("cc-exec-guard.audit")
    audit_logger.info(json.dumps({"a": 1}))
    audit_logger.info(json.dumps({"b": 2}))
    raw = _read(os.path.join(log_dir, "audit.jsonl"))
    lines = raw.splitlines()
    assert len(lines) == 2  # 严格一行一条，无空行
    assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": 2}]


def test_handlers_honor_config(tmp_path):
    d = str(tmp_path / "logs2")
    setup_logging(
        {
            "log_dir": d,
            "app_level": "DEBUG",
            "app_when": "midnight",
            "app_interval": 1,
            "app_backup_count": 7,
            "audit_max_bytes": 1234,
            "audit_backup_count": 3,
        }
    )
    try:
        app_logger = logging.getLogger("cc-exec-guard")
        assert app_logger.level == logging.DEBUG
        timed = [
            h
            for h in app_logger.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert timed and timed[0].backupCount == 7
        audit_logger = logging.getLogger("cc-exec-guard.audit")
        sized = [
            h for h in audit_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert sized and sized[0].maxBytes == 1234 and sized[0].backupCount == 3
    finally:
        setup_logging({"log_dir": SERVER_TMP_LOG})


def test_gzip_rotator_roundtrip(tmp_path):
    src = tmp_path / "app.log.1"
    src.write_text("hello-log", encoding="utf-8")
    dest = str(tmp_path / "app.log.1")
    _gzip_rotator(str(src), dest)
    assert not src.exists()
    assert _gzip_namer(dest) == dest + ".gz"
    with gzip.open(dest + ".gz", "rt", encoding="utf-8") as f:
        assert f.read() == "hello-log"


def _load_gate(monkeypatch, tmp_path, max_bytes="200", backups="3"):
    log_path = str(tmp_path / "gate.jsonl")
    monkeypatch.setenv("CC_EXEC_GUARD_GATE_LOG", log_path)
    monkeypatch.setenv("CC_EXEC_GUARD_GATE_MAX_BYTES", max_bytes)
    monkeypatch.setenv("CC_EXEC_GUARD_GATE_BACKUPS", backups)
    hook = os.path.join(os.getcwd(), "plugins", "claude", "scripts", "gate.py")
    spec = importlib.util.spec_from_file_location("gate_hook_under_test", hook)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, log_path


def test_gate_rotation_caps_backups_and_gzips(monkeypatch, tmp_path):
    mod, log_path = _load_gate(monkeypatch, tmp_path)
    for i in range(30):
        mod._gate_log({"i": i, "pad": "x" * 80})
    backups = sorted(str(p) for p in tmp_path.glob("gate.jsonl.*"))
    assert 1 <= len(backups) <= 3, backups  # 归档总数封顶
    assert all(p.endswith(".gz") for p in backups)  # 归档全部 gzip
    assert os.path.exists(log_path)
    # 最新写入的行必须在当前文件里
    cur = _read(log_path).strip().splitlines()
    assert json.loads(cur[-1])["i"] == 29
    # 旧归档可解压且内容是 JSON 行
    with gzip.open(backups[0], "rt", encoding="utf-8") as f:
        for line in f.read().strip().splitlines():
            assert "pad" in json.loads(line)


def test_gate_rotation_tolerates_legacy_plain_backup(monkeypatch, tmp_path):
    mod, log_path = _load_gate(monkeypatch, tmp_path)
    mod._gate_log({"warm": 1})
    with open(log_path + ".1", "w", encoding="utf-8") as f:  # 老版本留下的无压缩归档
        f.write('{"legacy": true}\n')
    for i in range(10):  # 触发归档，不应抛异常
        mod._gate_log({"i": i, "pad": "y" * 80})
    backups = sorted(str(p) for p in tmp_path.glob("gate.jsonl.*"))
    assert len(backups) <= 3
