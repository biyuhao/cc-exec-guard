"""cc-exec-guard 常驻服务：单进程常驻，同主机多会话共享。

API:
  POST /v1/classify  {command, cwd, session_id, tool} -> {decision, confidence, reason, request_id}
  GET  /healthz
  GET  /metrics (简易计数器)

日志（关键）：
  每条 classify 写两处：
    1) app.log    精简一行：request_id decision latency backend（排障用）
    2) audit.jsonl 完整结构化 JSON（审计/复盘/微调回流用），含脱敏 preview + hash，不存密钥原文
"""

import json
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .backends.laya import LayaBackend
from .backends.rules import RulesBackend
from .logging_setup import setup_logging
from .redact import preview, sha256_hex

CFG_PATH = Path(__file__).resolve().parent.parent / "config.example.yaml"


def load_cfg():
    import os

    override = os.environ.get("CC_EXEC_GUARD_CONFIG", "")
    path = Path(override).expanduser() if override else CFG_PATH
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not cfg:
        cfg = {
            "logging": {},
            "thresholds": {"deny": 0.75, "allow": 0.85},
            "policy": {"default": "ask"},
            "service": {"host": "127.0.0.1", "port": 3777},
        }
    # 日志目录允许环境变量覆盖（测试/容器/沙箱场景）
    log_dir_env = os.environ.get("CC_EXEC_GUARD_LOG_DIR", "")
    if log_dir_env:
        cfg.setdefault("logging", {})["log_dir"] = log_dir_env
    return cfg


CFG = load_cfg()
APP_LOG, AUDIT_LOG = setup_logging(CFG.get("logging", {}))

app = FastAPI(title="cc-exec-guard")
rules = RulesBackend()
_backend_cfg = CFG.get("backend", {})
laya = LayaBackend(
    model_id=_backend_cfg.get("model_id") or None,
    device=_backend_cfg.get("device") or None,
    mode=_backend_cfg.get("mode") or None,
    checkpoint=_backend_cfg.get("checkpoint") or None,
)

# 启动即热加载：有本地权重就常驻，没有则降级为桩并记一条日志。
# 只读本地（绝不在启动时触发网络下载），失败绝不阻止服务启动，
# 让第一条请求不再付出 ~4s 的冷加载。
try:
    if laya.warmup(download=False):
        APP_LOG.info("laya warmed up: %s", laya.status())
    else:
        APP_LOG.info("laya stub active (no local weights): %s", laya.status().get("last_error"))
except Exception as e:  # noqa: BLE001 -- 预热失败只告警，请求路径自有降级兜底
    APP_LOG.warning("laya warmup failed, continuing degraded: %s", e)
TH = CFG.get("thresholds", {"deny": 0.75, "allow": 0.85})

_counters = {"total": 0, "allow": 0, "ask": 0, "deny": 0, "fallback": 0}


class ClassifyReq(BaseModel):
    command: str = ""
    cwd: str = ""
    session_id: str = ""
    tool: str = "Bash"


def _map_to_decision(label: str, conf: float) -> str:
    if label == "dangerous" or conf >= 99:  # 规则后端 confidence=1.0 直接 deny
        pass
    if label == "dangerous" and conf >= TH.get("deny", 0.75):
        return "deny"
    if label == "safe" and conf >= TH.get("allow", 0.85):
        return "allow"
    if label == "dangerous":
        return "ask"  # 高危但置信度不够 -> 转人工，不直接放行
    return {"safe": "allow", "needs_confirmation": "ask", "dangerous": "deny"}.get(label, "ask")


@app.post("/v1/classify")
async def classify(req: ClassifyReq, request: Request):
    t0 = time.perf_counter()
    request_id = uuid.uuid4().hex[:12]
    cmd = req.command or ""
    cmd_hash = sha256_hex(cmd)
    cmd_preview, truncated = preview(cmd)

    try:
        # 1) 硬规则先行
        r = rules.classify(cmd, {"cwd": req.cwd})
        backend_chain = ["rules"]
        if r is None:
            # 2) Laya（可替换 backend）兜底语义判断
            r = laya.classify(cmd, {"cwd": req.cwd})
            backend_chain.append("laya")

        decision = _map_to_decision(r.label, r.confidence)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        _counters["total"] += 1
        _counters[decision] += 1

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": request_id,
            "session_id": req.session_id,
            "tool": req.tool,
            "cwd": req.cwd,
            "command_hash": cmd_hash,
            "command_preview": cmd_preview,
            "command_truncated": truncated,
            "command_len": len(cmd),
            "backend": r.backend,
            "backend_chain": backend_chain,
            "model_version": r.model_version,
            "label": r.label,
            "confidence": round(r.confidence, 4),
            "probs": {k: round(float(v), 4) for k, v in r.probs.items()},
            "rule_hits": r.rule_hits,
            "decision": decision,
            "reason": r.reason,  # 为什么 allow/ask/deny，人可读
            "thresholds": TH,
            "latency_ms": latency_ms,
            "client_ip": request.client.host if request.client else "",
        }
        AUDIT_LOG.info(json.dumps(record, ensure_ascii=False))
        APP_LOG.info(
            "%s conf=%.2f backend=%s %dms hash=%s cwd=%s preview=%s",
            decision,
            r.confidence,
            r.backend,
            latency_ms,
            cmd_hash[:10],
            req.cwd,
            cmd_preview[:120],
            extra={"request_id": request_id},
        )
        return {
            "decision": decision,
            "confidence": r.confidence,
            "reason": r.reason,
            "request_id": request_id,
            "label": r.label,
            "latency_ms": latency_ms,
        }
    except Exception as e:  # noqa: BLE001 -- 绝不 500 给 hook：降级为 ask，保证 fail-closed
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        _counters["fallback"] += 1
        APP_LOG.exception(
            "classify error, fallback to ask: %s", e, extra={"request_id": request_id}
        )
        AUDIT_LOG.info(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "request_id": request_id,
                    "session_id": req.session_id,
                    "tool": req.tool,
                    "cwd": req.cwd,
                    "command_hash": cmd_hash,
                    "command_preview": cmd_preview,
                    "decision": "ask",
                    "reason": f"cc-exec-guard 内部异常，已降级为人工确认: {type(e).__name__}",
                    "fallback": True,
                    "latency_ms": latency_ms,
                },
                ensure_ascii=False,
            )
        )
        return JSONResponse(
            {
                "decision": "ask",
                "confidence": 0.0,
                "reason": "cc-exec-guard 异常，已降级为人工确认",
                "request_id": request_id,
            },
            status_code=200,
        )


@app.get("/healthz")
async def healthz():
    return {"ok": True, "counters": _counters, "laya": laya.status()}


@app.get("/metrics")
async def metrics():
    return _counters
