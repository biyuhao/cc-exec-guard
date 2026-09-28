"""Laya backend：官方 laya 包 + 本地权重推理，失败时启发式兜底。

调用的是 English checkpoint（ModernBERT-large, 421M，非自回归）：对每条命令
提一个三选一 choice 问题（safe / needs_confirmation / dangerous），单次前向
返回校准概率。命令是拉丁字符 Shell 文本，固定走 english，不经过 Router 的
多语言分发（避免触发 multilingual 权重下载）。

权重来源（三选一，按优先序）：
  1. 构造参数 checkpoint / 环境变量 CC_EXEC_GUARD_LAYA_DIR / 配置 backend.checkpoint
     指向的本地目录（魔搭下载：make model-download，见 Makefile）。
  2. 上面都没配时用 HF repo convaiinnovations/laya，走 huggingface_hub 缓存。
     auto 模式下强制离线（HF_HUB_OFFLINE=1），有缓存就用，没有就降级，绝不
     在请求路径上触发网络下载；HF 镜像站用户设 HF_ENDPOINT 即可。
  3. 都没有 → 启发式桩（行为与旧版一致，reason 前缀 [laya-stub] 以示区分）。

三档模式（构造参数 / CC_EXEC_GUARD_LAYA_MODE）：
  auto  默认。有权重就真实前向，否则静默降级。
  stub  永远启发式（测试/离线/排障）。
  real  强制真实推理（允许联网下载），失败抛异常给上层转 ask。

预热（网络可用时拉取，无网络时只验本地）：
  make model-download && make model-warmup
"""

import importlib.util
import logging
import os
import threading

from .base import Backend, ClassifyResult

LOG = logging.getLogger("cc-exec-guard.laya")

LABELS = ("safe", "needs_confirmation", "dangerous")
STUB_VERSION = "stub-1.0"

DEFAULT_MODEL_ID = "convaiinnovations/laya"

RISK_QUESTION = {
    "risk": {
        "type": "choice",
        "instructions": (
            "You are a shell command risk gate. Classify the risk of executing this shell command."
        ),
        "criteria": {
            # 措辞经 10 条命令电池验证（argmax 10/10 正确）：抽象描述比堆例句好，
            # 例句会把模型吸到 safe，见 README 的 Laya 一节
            "safe": "Read-only or fully reversible routine work: listing files, viewing "
            "contents, checking status, compiling, running tests. No deletion, no "
            "modification of files, permissions or system state, no network "
            "download-and-execute, no privilege escalation, no secrets leaving "
            "the machine.",
            "needs_confirmation": "State-changing or hard-to-reverse actions a careful "
            "engineer would double check: deleting or overwriting files, moving data, "
            "changing permissions, installing packages, pushing to shared branches, "
            "touching production, cloud or shared resources.",
            "dangerous": "Likely destructive or hostile: deleting system or home "
            "directories, wiping disks or filesystems, fork bombs, exfiltrating "
            "secrets, piping remote downloads into a shell, privilege escalation "
            "exploits, mass encryption or deletion.",
        },
    }
}

# 启发式兜底：无权重/无依赖时保证全链路可用、可评估
RISKY_HINTS = [
    "rm -rf",
    "chmod -R",
    "chown -R",
    "dd ",
    "/dev/",
    "kubectl delete",
    "drop database",
    "curl",
    "wget",
    "| sh",
    "| bash",
    "sudo ",
]


def _has_pkg(name):
    return importlib.util.find_spec(name) is not None


def _resolve_device(device):
    d = (device or os.environ.get("CC_EXEC_GUARD_DEVICE", "") or "auto").strip().lower()
    if d in ("cpu", "cuda", "mps", "xpu"):
        return d
    return None  # auto：交给 laya Agent（含 cuda→cpu 回退）


class LayaBackend(Backend):
    name = "laya"
    version = STUB_VERSION  # 类属性保持旧值兼容；实例按是否加载真实权重动态上报

    def __init__(self, model_id=None, device=None, mode=None, checkpoint=None):
        self.model_id = (
            model_id or os.environ.get("CC_EXEC_GUARD_MODEL_ID", "") or DEFAULT_MODEL_ID
        ).strip()
        self.checkpoint = (checkpoint or os.environ.get("CC_EXEC_GUARD_LAYA_DIR", "") or "").strip()
        self.device = _resolve_device(device)
        self.mode = (mode or os.environ.get("CC_EXEC_GUARD_LAYA_MODE", "") or "auto").strip().lower()
        if self.mode not in ("auto", "stub", "real"):
            LOG.warning("unknown CC_EXEC_GUARD_LAYA_MODE=%r, fallback to auto", self.mode)
            self.mode = "auto"
        self._lock = threading.Lock()
        self._agent = None
        self._checkpoint_used = ""
        self._last_error = ""
        self._no_deps = False  # 缺 laya/torch 时进程级永久降级（装完需重启 cc-exec-guard）

    # -- 状态上报（给 /healthz，绝不抛异常） --
    def status(self):
        try:
            return {
                "mode": self.mode,
                "model_id": self.model_id,
                "checkpoint": self._checkpoint_used or self.checkpoint or None,
                "device": self.device or "auto",
                "loaded": self._agent is not None,
                "model_version": self._version(),
                "last_error": self._last_error,
            }
        except Exception:  # noqa: BLE001
            return {"mode": self.mode, "loaded": False, "last_error": "status failed"}

    def _version(self):
        if self._agent is not None:
            short = (
                (self._checkpoint_used or self.model_id).split("/")[-1].lower().replace("_", "-")
            )
            return f"{short}-real"
        return STUB_VERSION

    # -- 加载 --
    def warmup(self, download=False):
        """强制加载一次。download=True 允许联网拉取；mode=stub 时直接返回 False。"""
        if self.mode == "stub":
            return False
        with self._lock:
            if self._agent is not None:
                return True
            self._load(offline=not download)
            return self._agent is not None

    def _ensure_loaded(self):
        if self.mode == "stub" or self._agent is not None or self._no_deps:
            return self._agent is not None
        with self._lock:
            if self._agent is not None or self._no_deps:
                return self._agent is not None
            try:
                # auto 只读本地（含 HF 离线缓存）：无权重瞬间失败，不触发网络下载
                self._load(offline=True)
            except ImportError as e:
                self._no_deps = True
                self._last_error = f"{type(e).__name__}: {e}"
                LOG.warning("laya deps missing (%s), stub for this process", self._last_error)
            except Exception as e:  # noqa: BLE001 -- 加载失败就降级，不挡分类主路径
                self._last_error = f"{type(e).__name__}: {e}"
                LOG.warning("laya weights unavailable (%s), using stub; " "run make service-install " "(auto-downloads + wires checkpoint) or set backend.checkpoint / " "CC_EXEC_GUARD_LAYA_DIR", self._last_error)
            return self._agent is not None

    def _load(self, offline):
        if not _has_pkg("laya"):
            raise ImportError("No module named 'laya' (pip install laya / make add-laya)")
        from laya import Agent  # 延迟导入：跑桩不需要 laya 包

        target = self.checkpoint or self.model_id
        if self.checkpoint and not os.path.isdir(self.checkpoint):
            raise FileNotFoundError(f"laya checkpoint dir not found: {self.checkpoint}")

        if offline:
            # 本地目录直接用；HF id 只允许命中离线缓存，禁止网络
            if os.path.isdir(target):
                agent = Agent(target, device=self.device)
            else:
                prev = os.environ.get("HF_HUB_OFFLINE")
                os.environ["HF_HUB_OFFLINE"] = "1"
                try:
                    agent = Agent(target, device=self.device)
                finally:
                    if prev is None:
                        os.environ.pop("HF_HUB_OFFLINE", None)
                    else:
                        os.environ["HF_HUB_OFFLINE"] = prev
        else:
            agent = Agent(target, device=self.device)  # 允许联网下载（HF 或 HF_ENDPOINT 镜像）

        self._agent = agent
        self._checkpoint_used = target
        self._last_error = ""
        LOG.info("laya real weights loaded: %s", target)

    # -- 推理 --
    def _infer_real(self, command, ctx):
        state = f"cwd: {(ctx or {}).get('cwd', '')}\ncommand: {command or ''}"
        out = self._agent.system_one(state, RISK_QUESTION)
        ans = (out.get("answers") or {}).get("risk") or {}
        probs = {k: float((ans.get("probabilities") or {}).get(k, 0.0)) for k in LABELS}
        if sum(probs.values()) <= 0:
            raise ValueError(f"laya returned no probabilities: {ans}")
        label = ans.get("choice") or max(probs, key=probs.get)
        if label not in LABELS:
            raise ValueError(f"laya returned unknown choice: {label!r}")
        conf = float(ans.get("answer_confidence") or probs[label])
        return label, conf, probs

    def _infer_stub(self, command, ctx):
        c = (command or "").lower()
        hits = sum(1 for h in RISKY_HINTS if h in c)
        if hits >= 2:
            return "dangerous", 0.82, {"dangerous": 0.82, "needs_confirmation": 0.15, "safe": 0.03}
        if hits == 1:
            return (
                "needs_confirmation",
                0.65,
                {"needs_confirmation": 0.65, "safe": 0.30, "dangerous": 0.05},
            )
        return "safe", 0.93, {"safe": 0.93, "needs_confirmation": 0.06, "dangerous": 0.01}

    def classify(self, command: str, ctx: dict) -> ClassifyResult:
        real = False
        if self.mode == "real":
            # real 模式：加载失败不静默降级，抛给上层转 ask（fail-closed 且可审计）
            with self._lock:
                if self._agent is None:
                    self._load(offline=False)
            label, conf, probs = self._infer_real(command, ctx)
            real = True
        elif self._ensure_loaded():
            try:
                label, conf, probs = self._infer_real(command, ctx)
                real = True
            except Exception as e:  # noqa: BLE001 -- 推理期异常也降级为桩，不抛 500
                LOG.warning("laya real inference failed (%s), stub fallback", e)
                label, conf, probs = self._infer_stub(command, ctx)
        else:
            label, conf, probs = self._infer_stub(command, ctx)

        tag = self.name if real else f"{self.name}-stub"
        reason = {
            "safe": f"[{tag}] 判定安全，置信度 {conf:.2f}，动作为只读/常规开发命令",
            "needs_confirmation": f"[{tag}] 语义可疑（置信度 {conf:.2f}），"
            "含风险关键词或跨目录写操作，需人工确认",
            "dangerous": f"[{tag}] 判定高危（置信度 {conf:.2f}），命中破坏/外联/提权语义",
        }[label]
        return ClassifyResult(label, conf, probs, reason, self.name, self._version(), [])
