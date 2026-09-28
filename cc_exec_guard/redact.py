"""命令脱敏 + 哈希 + 预览截断。审计只存 preview + hash，不存全量密钥。"""
import hashlib
import re

PREVIEW_LIMIT = 1000

_PATTERNS = [
    # private key block
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,2000}?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # github / openai / aws / generic token
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"sk-(proj-)?[A-Za-z0-9\-_]{10,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    # k=v 形式密码
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\";]+['\"]?"),
     r"\1=[REDACTED]"),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]{10,}"), "Bearer [REDACTED]"),
    # 纯长 hex/base64 疑似密钥（保守：只处理 --token xxx 这类显式上下文由调用方决定，此处不 blanket 替换）
]


def redact(text: str) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def preview(text: str, limit: int = PREVIEW_LIMIT) -> tuple[str, bool]:
    """返回 (redacted_preview, truncated)。"""
    r = redact(text or "")
    if len(r) > limit:
        return r[:limit] + f"...[TRUNCATED total={len(r)}]", True
    return r, False
