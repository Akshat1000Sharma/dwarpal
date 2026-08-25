"""Shared request dependencies."""

from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.connect import service as connections
from app.correlation import get_correlation_id
from app.db.base import SessionFactory
from app.db.models import AgentConnection, ConnectionScope
from app.settings import settings

CONNECTION_HEADER = "X-Dwarpal-Connection"


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


def buyer_connection(
    db: Session = Depends(get_db),
    x_dwarpal_connection: str | None = Header(default=None, alias=CONNECTION_HEADER),
) -> AgentConnection | None:
    """The connection this request presented, if it presented a live buyer one.

    A connection names who the agent belongs to and where to send them a receipt. It is never
    consulted for authority.
    """
    connection = connections.resolve(db, x_dwarpal_connection, scope=ConnectionScope.BUYER)
    if connection is not None:
        connections.touch(db, connection)
    return connection


def agent_identifier(
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    connection: AgentConnection | None = Depends(buyer_connection),
) -> str:
    """The identifier an unverified agent claims, used only for hold quotas and rate limits.

    It is never trusted for authority. Authority comes from the credential chain, where the agent
    identity is the key the mandate was issued to. A connection token pins the identifier to the
    one its owner registered, so a caller cannot spread its holds across invented names.
    """
    if connection is not None:
        return connection.agent_id
    return x_agent_id or "agent:anonymous"


def require_merchant_token(
    db: Session = Depends(get_db),
    x_merchant_token: str | None = Header(default=None, alias="X-Merchant-Token"),
    x_dwarpal_connection: str | None = Header(default=None, alias=CONNECTION_HEADER),
) -> None:
    """Guard the merchant control plane.

    These endpoints revoke a human's mandate, stop an agent, widen a spend limit and close a money
    discrepancy, and the documented runbook tunnels this port to the public internet so webhooks
    can arrive. An unset token refuses rather than serves open, so a misconfiguration cannot
    quietly publish the control plane.

    A merchant-scoped connection token is accepted as well, so somebody can point their own agent
    at running the shop without being handed the shared secret. It is checked against the database
    on every request, so revoking one takes effect immediately.
    """
    expected = settings.MERCHANT_API_TOKEN
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MERCHANT_API_TOKEN is not configured, so the merchant surface is closed",
        )
    if x_merchant_token and hmac.compare_digest(x_merchant_token, expected):
        return
    merchant_connection = connections.resolve(
        db, x_dwarpal_connection, scope=ConnectionScope.MERCHANT
    )
    if merchant_connection is not None:
        connections.touch(db, merchant_connection)
        return
    raise HTTPException(
        status_code=401,
        detail="a valid X-Merchant-Token or merchant-scoped X-Dwarpal-Connection is required",
    )
