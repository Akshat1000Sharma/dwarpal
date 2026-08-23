"""Correlation identifier propagated across logs, records and evidence packets."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_CORRELATION_ID: ContextVar[str | None] = ContextVar("dwarpal_correlation_id", default=None)

HEADER = "X-Correlation-Id"


def new_correlation_id() -> str:
    return f"dwc_{uuid.uuid4().hex}"


def set_correlation_id(value: str) -> None:
    _CORRELATION_ID.set(value)


def get_correlation_id() -> str:
    value = _CORRELATION_ID.get()
    if value is None:
        value = new_correlation_id()
        _CORRELATION_ID.set(value)
    return value


def reset_correlation_id() -> None:
    _CORRELATION_ID.set(None)
