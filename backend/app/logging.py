"""Structured JSON logging with correlation ids and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from app.correlation import get_correlation_id
from app.settings import settings

_SECRET_FIELDS = re.compile(
    r"(?i)(secret|password|token|api_key|authorization|signature|private)"
)
_REDACTED = "[redacted]"


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking fields and truncate credential blobs."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_FIELDS.search(str(key)):
                out[key] = _REDACTED
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 256:
        return value[:128] + f"...[{len(value)} chars]"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload["context"] = redact(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.LOG_LEVEL)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_context(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    logger.log(level, message, extra={"context": context})
