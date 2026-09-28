#!/usr/bin/env python3
"""Claude PreToolUse hook：只做转发，不加载模型，热路径 <5ms + 网络。

stdin: Claude 传的 JSON {tool_input: {command}, session_id, cwd}
stdout: JSON 给 Claude {hookSpecificOutput: {permissionDecision, reason}} 或 exit 2
本地日志：~/.cc-exec-guard/logs/gate.jsonl，极简追加 + 自转归档
（超 CC_EXEC_GUARD_GATE_MAX_BYTES 默认 5MB 即 gzip 为 .1.gz，
最多保留 CC_EXEC_GUARD_GATE_BACKUPS 默认 5 个归档），
  完整审计以 cc-exec-guard 的 audit.jsonl 为准，用 request_id 关联两边。
降级：cc-exec-guard 不可达/超时一律 ask（fail-closed），绝不静默 allow。
"""
import json
import os
import sys
import time
import urllib.request

GUARD_URL = os.environ.get("CC_EXEC_GUARD_URL", "http://127.0.0.1:3777/v1/classify")
TIMEOUT = float(os.environ.get("CC_EXEC_GUARD_TIMEOUT", "4"))
LOG_PATH = os.environ.get(
    "CC_EXEC_GUARD_GATE_LOG",
    os.path.join(os.path.expanduser("~"), ".cc-exec-guard", "logs", "gate.jsonl"),
)
# hook 端滚动策略：超限即 gzip 归档，归档总数封顶：
#   gate.jsonl -> gate.jsonl.1.gz -> ... -> gate.jsonl.N.gz，最旧的丢弃
GATE_MAX_BYTES = int(os.environ.get("CC_EXEC_GUARD_GATE_MAX_BYTES", str(5 * 1024 * 1024)))
GATE_BACKUPS = max(1, int(os.environ.get("CC_EXEC_GUARD_GATE_BACKUPS", "5")))


def _gzip_file(src: str, dest_gz: str) -> None:
    import gzip
    import shutil

    with open(src, "rb") as f_in, gzip.open(dest_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(src)


def _rotate_gate_log() -> None:
    """达到 GATE_MAX_BYTES 就归档：当前文件 gzip 为 .1.gz，旧归档依次后移，
    超出 GATE_BACKUPS 的最旧归档直接丢弃。只在超限时执行，热路径仅一次 getsize。"""
    try:
        if os.path.getsize(LOG_PATH) <= GATE_MAX_BYTES:
            return
    except OSError:
        return  # 文件不存在或不可读：本次不归档，绝不阻塞 hook 主路径
    try:
        for suffix in ("", ".gz"):  # 兼容老版本留下的无压缩 .N 归档
            try:
                os.remove(f"{LOG_PATH}.{GATE_BACKUPS}{suffix}")
            except OSError:
                pass
        for i in range(GATE_BACKUPS - 1, 0, -1):
            for suffix in (".gz", ""):
                src = f"{LOG_PATH}.{i}{suffix}"
                if os.path.exists(src):
                    os.rename(src, f"{LOG_PATH}.{i + 1}{suffix}")
                    break
        _gzip_file(LOG_PATH, f"{LOG_PATH}.1.gz")
    except OSError:
        pass  # 沙箱只读等环境下归档失败：本次跳过，下次写入前再试


def _gate_log(obj: dict):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        _rotate_gate_log()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001,S110 -- 本地日志 best-effort，绝不能拖累 hook 主路径
        pass


def main():
    t0 = time.perf_counter()
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 -- stdin 解析失败就当空事件，后续走 ask 降级
        ev = {}
    cmd = ((ev.get("tool_input") or {}).get("command") or "").strip()
    session_id = ev.get("session_id", "")
    cwd = ev.get("cwd", "") or ((ev.get("tool_input") or {}).get("cwd", ""))

    if not cmd:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": "未取到命令内容，已降级为人工确认"}}))
        return

    body = json.dumps({"command": cmd, "cwd": cwd, "session_id": session_id, "tool": "Bash"}).encode()
    try:
        req = urllib.request.Request(GUARD_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        decision = data.get("decision", "ask")
        reason = data.get("reason", "")
        request_id = data.get("request_id", "")
        _gate_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "request_id": request_id,
                   "session_id": session_id, "decision": decision,
                   "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,  # allow | ask | deny
            "permissionDecisionReason": f"{reason} [guard:{request_id}]"}}))
    except Exception as e:  # noqa: BLE001 -- cc-exec-guard 不可达/超时一律转人工，fail-closed
        # cc-exec-guard 挂了/超时：转人工，日志留痕
        _gate_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session_id": session_id,
                   "decision": "ask", "fallback": True, "error": f"{type(e).__name__}: {e}",
                   "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"cc-exec-guard 不可达，已降级为人工确认（{type(e).__name__}），命令稍后重试"}}))


if __name__ == "__main__":
    main()
