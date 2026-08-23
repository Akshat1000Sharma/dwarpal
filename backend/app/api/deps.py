"""Shared request dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, Request
from sqlalchemy.orm import Session

from app.correlation import get_correlation_id
from app.db.base import SessionFactory


def get_db() -> Iterator[Session]:
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def correlation_id(request: Request) -> str:
    del request
    return get_correlation_id()


def agent_identifier(
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> str:
    """The identifier an unverified agent claims, used only for hold quotas and rate limits.

    It is never trusted for authority. Authority comes from the credential chain, where the agent
    identity is the key the mandate was issued to.
    """
    return x_agent_id or "agent:anonymous"
