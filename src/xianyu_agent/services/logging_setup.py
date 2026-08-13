"""daemon 文件日志与敏感字段脱敏。"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"(?i)(cookie\s*[:=]\s*)(.*)"),
    re.compile(
        r"(?i)((?:unb|cookie2|_m_h5_tk|_m_h5_tk_enc|token|fernet_key)"
        r"\s*[:=]\s*)([^\s;]+)"
    ),
    re.compile(r"(?i)((?:code|sent_text|reply_text|content)\s*[:=]\s*)(.*)"),
)


def redact_text(value: str) -> str:
    """遮蔽日志中常见的 Cookie、Token、卡密和消息正文。"""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1<redacted>", redacted)
    return redacted


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact_text(message)
        record.args = ()
        return True


def configure_daemon_logging(log_path: Path, *, level: str) -> None:
    """为 root logger 配置可轮转、脱敏的 daemon 文件日志。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_xianyu_daemon_handler", False):
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler._xianyu_daemon_handler = True  # type: ignore[attr-defined]
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
