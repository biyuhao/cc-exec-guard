"""服务端冒烟：必须在 import cc_exec_guard.server 之前设置 CC_EXEC_GUARD_LOG_DIR。"""
import json
import os

import pytest

TMP_LOG = os.path.join(os.getcwd(), ".tmp-logs-test")
os.environ["CC_EXEC_GUARD_LOG_DIR"] = TMP_LOG
os.makedirs(TMP_LOG, exist_ok=True)

from fastapi.testclient import TestClient

from cc_exec_guard.server import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_classify_allow_and_audit(client):
    r = client.post("/v1/classify", json={"command": "ls -la", "cwd": "/tmp"})
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "allow"
    assert data["request_id"]

    audit_path = os.path.join(TMP_LOG, "audit.jsonl")
    assert os.path.exists(audit_path)
    from pathlib import Path
    lines = Path(audit_path).read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["request_id"] == data["request_id"]
    assert rec["decision"] == "allow" and rec["reason"]
    assert rec["command_len"] == len("ls -la")  # audit 记录含原文长度


def test_classify_deny(client):
    r = client.post("/v1/classify", json={"command": "rm -rf / ", "cwd": "/"})
    assert r.json()["decision"] == "deny"


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True
