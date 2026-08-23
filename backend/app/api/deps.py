"""Shared request dependencies."""

from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.correlation import get_correlation_id
from app.db.base import SessionFactory
from app.settings import settings


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


def require_merchant_token(
    x_merchant_token: str | None = Header(default=None, alias="X-Merchant-Token"),
) -> None:
    """Guard the merchant control plane.

    These endpoints revoke a human's mandate, stop an agent, widen a spend limit and close a money
    discrepancy, and the documented runbook tunnels this port to the public internet so webhooks
    can arrive. An unset token refuses rather than serves open, so a misconfiguration cannot
    quietly publish the control plane.
    """
    expected = settings.MERCHANT_API_TOKEN
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MERCHANT_API_TOKEN is not configured, so the merchant surface is closed",
        )
    if not x_merchant_token or not hmac.compare_digest(x_merchant_token, expected):
        raise HTTPException(status_code=401, detail="a valid X-Merchant-Token is required")
