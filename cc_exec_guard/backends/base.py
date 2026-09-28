"""Backend 抽象：Laya 只是其中一种实现，可替换。"""
from dataclasses import dataclass, field


@dataclass
class ClassifyResult:
    label: str  # safe | needs_confirmation | dangerous
    confidence: float
    probs: dict = field(default_factory=dict)  # {label: prob}
    reason: str = ""          # 人可读的放行/拦截原因，hook 会透传给用户
    backend: str = ""         # rules | laya | llm | ensemble
    model_version: str = ""
    rule_hits: list = field(default_factory=list)


class Backend:
    name = "base"
    version = "0"

    def classify(self, command: str, ctx: dict) -> ClassifyResult:
        raise NotImplementedError
