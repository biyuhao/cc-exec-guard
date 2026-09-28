"""硬规则 backend：先于模型，零延迟，高 precision。"""
import re

from .base import Backend, ClassifyResult

DENY_RULES = [
    ("destructive_root", re.compile(r"\brm\s+-rf?\s+/(?:\s|$)"), "rm -rf / 根目录删除"),
    ("destructive_home", re.compile(r"\brm\s+-rf?\s+~/?(?:\s|$)"), "rm -rf ~ 家目录删除"),
    ("fork_bomb", re.compile(r":\(\)\s*\{\s*:\|\:&\s*\};:"), "fork bomb"),
    ("mkfs", re.compile(r"\bmkfs(?:\.\w+)?\b"), "mkfs 格式化"),
    ("dd_disk", re.compile(r"\bdd\s+.*of=/dev/(sd|hd|nvme)"), "dd 写裸设备"),
    ("curl_pipe_sh", re.compile(r"curl\s+.+\|\s*(sh|bash)\b"), "curl | sh 远程执行"),
    ("wget_pipe_sh", re.compile(r"wget\s+.+\|\s*(sh|bash)\b"), "wget | sh 远程执行"),
    ("chmod_root", re.compile(r"chmod\s+-R\s+777\s+/"), "chmod -R 777 /"),
]

ASK_RULES = [
    ("prod_delete", re.compile(r"kubectl\s+delete\s+(ns|namespace|cluster)"), "k8s 删除 ns/cluster"),
    ("s3_rm", re.compile(r"aws\s+s3\s+rm\s+s3://"), "s3 删除"),
    ("git_push_force", re.compile(r"git\s+push\s+.*--force"), "git force push"),
    ("db_drop", re.compile(r"(?i)\bdrop\s+(database|table)\b"), "drop database/table"),
]


class RulesBackend(Backend):
    name = "rules"
    version = "1.0"

    def classify(self, command: str, ctx: dict):
        cmd = command or ""
        for rid, pat, desc in DENY_RULES:
            if pat.search(cmd):
                return ClassifyResult("dangerous", 1.0,
                                      {"dangerous": 1.0, "safe": 0.0, "needs_confirmation": 0.0},
                                      f"硬规则拦截[{rid}]：{desc}", self.name, self.version, [rid])
        for rid, pat, desc in ASK_RULES:
            if pat.search(cmd):
                return ClassifyResult("needs_confirmation", 0.9,
                                      {"needs_confirmation": 0.9, "safe": 0.1, "dangerous": 0.0},
                                      f"硬规则复核[{rid}]：{desc}，需人工确认", self.name, self.version, [rid])
        return None  # 无命中则交给下一个 backend
