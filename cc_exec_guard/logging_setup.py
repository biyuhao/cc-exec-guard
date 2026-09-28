import gzip
import logging
import logging.handlers
import os
from pathlib import Path


def _gzip_namer(default_name: str) -> str:
    # RotatingFileHandler namer: app.log.1 -> app.log.1.gz
    return default_name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(_gzip_namer(dest), "wb") as f_out:
        f_out.writelines(f_in)
    os.remove(source)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)  # 命令原文可能含密钥，目录收紧权限
    except OSError:
        pass
    return path


class JsonLineFormatter(logging.Formatter):
    """audit 日志只负责透传 dict 已序列化好的字符串，不做二次格式化。

    注意：不要在这里补 "\\n"。Handler.emit 写出时会自动追加
    terminator（默认 "\\n"），手动加换行会导致每行后面多一个空行。
    """
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class _RequestIdFilter(logging.Filter):
    """保证每条 app 日志都有 request_id：请求路径由调用方经 extra 传入，
    启动日志等无请求上下文的记为 "-"。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = "-"
        return True


def setup_logging(cfg: dict):
    """
    cfg 示例见 config.example.yaml 的 logging 段：
      log_dir, app_level, app_when, app_backup_count,
      audit_max_bytes, audit_backup_count, to_stdout
    返回 (app_logger, audit_logger)。
    设计：
      app.log      人读，DEBUG/INFO，TimedRotating（默认每天一切，保留14份），用于 debug/排障
      audit.jsonl  机读，一行一个决策，SizeRotating（默认20MB x 10），用于审计/复盘/微调回流
      stdout       给 systemd-journald 看，只打 INFO+ 精简行
    """
    log_dir = _ensure_dir(Path(cfg.get("log_dir", "~/.cc-exec-guard/logs")).expanduser())

    # ---- app logger ----
    app_logger = logging.getLogger("cc-exec-guard")
    app_logger.setLevel(getattr(logging, cfg.get("app_level", "INFO").upper(), logging.INFO))
    app_logger.handlers.clear()
    app_logger.propagate = False
    app_logger.addFilter(_RequestIdFilter())

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s")

    app_path = log_dir / "app.log"
    app_handler = logging.handlers.TimedRotatingFileHandler(
        str(app_path),
        when=cfg.get("app_when", "midnight"),
        interval=int(cfg.get("app_interval", 1)),
        backupCount=int(cfg.get("app_backup_count", 14)),
        encoding="utf-8",
    )
    app_handler.setFormatter(fmt)
    app_handler.rotator = _gzip_rotator
    app_handler.namer = _gzip_namer
    app_logger.addHandler(app_handler)

    if cfg.get("to_stdout", True):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        app_logger.addHandler(sh)

    # ---- audit logger ----
    audit_logger = logging.getLogger("cc-exec-guard.audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.handlers.clear()
    audit_logger.propagate = False

    audit_path = log_dir / "audit.jsonl"
    audit_handler = logging.handlers.RotatingFileHandler(
        str(audit_path),
        maxBytes=int(cfg.get("audit_max_bytes", 20 * 1024 * 1024)),
        backupCount=int(cfg.get("audit_backup_count", 10)),
        encoding="utf-8",
    )
    audit_handler.setFormatter(JsonLineFormatter())
    audit_handler.rotator = _gzip_rotator
    audit_handler.namer = _gzip_namer
    audit_logger.addHandler(audit_handler)

    app_logger.info("logging ready dir=%s app=%s audit=%s", log_dir, app_path, audit_path)
    return app_logger, audit_logger
