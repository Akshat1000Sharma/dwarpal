"""Create, resolve and revoke agent connections.

The plaintext token exists exactly once, in the response to the request that created it. Only its
SHA-256 digest is stored, so a leaked database row cannot be replayed as a token, and a lost token
is replaced rather than recovered.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import AgentConnection, ConnectionScope
from app.settings import settings

TOKEN_PREFIX = "dwc_"
_TOKEN_BYTES = 24

# E.164: a leading plus, a non-zero country digit, then up to fourteen more digits.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class ConnectionError_(ValueError):
    """Raised when a connection cannot be created from what was supplied."""


def normalise_number(raw: str | None) -> str | None:
    """Accept a number the way a person types it, store it the way Meta requires it.

    Spaces, dashes and brackets are removed. Anything that is still not E.164 is refused rather
    than stored in a shape that would silently never deliver.
    """
    if raw is None or not raw.strip():
        return None
    cleaned = re.sub(r"[\s()\-.]", "", raw.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not _E164.match(cleaned):
        raise ConnectionError_(
            "the WhatsApp number must be in E.164 form, for example +919876543210"
        )
    return cleaned


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask(number: str | None) -> str | None:
    """Never return a full phone number to a client that did not supply it."""
    if not number:
        return None
    return f"{number[:3]}{'*' * max(0, len(number) - 7)}{number[-4:]}"


def _derive_agent_id(label: str) -> str:
    """An identifier of this connection's own, not of everyone who picked the same label.

    Receipts are routed by agent_id when a caller does not name a connection, so two people who
    both label theirs "shopping agent" must not collapse onto one identifier: the newest row would
    win the lookup and a stranger would be sent somebody else's purchase receipt. The suffix makes
    the derived identifier unique while keeping the label readable in the verdict log.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "agent"
    return f"agent:{slug[:96]}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class CreatedConnection:
    connection: AgentConnection
    token: str


def create_connection(
    session: Session,
    *,
    label: str,
    scope: ConnectionScope = ConnectionScope.BUYER,
    whatsapp: str | None = None,
    agent_id: str | None = None,
) -> CreatedConnection:
    label = (label or "").strip()
    if not label:
        raise ConnectionError_("a connection needs a label, so you can tell yours apart later")

    number = normalise_number(whatsapp)
    token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    row = AgentConnection(
        label=label[:120],
        scope=scope.value,
        agent_id=(agent_id or _derive_agent_id(label))[:128],
        whatsapp_e164=number,
        token_hash=digest(token),
        token_prefix=token[: len(TOKEN_PREFIX) + 6],
    )
    session.add(row)
    session.flush()
    return CreatedConnection(connection=row, token=token)


def resolve(
    session: Session, token: str | None, *, scope: ConnectionScope | None = None
) -> AgentConnection | None:
    """The live connection this token names, or None.

    A revoked connection resolves to None immediately, with no cache in the way, so revoking is
    effective on the very next request.
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = session.scalar(
        select(AgentConnection).where(AgentConnection.token_hash == digest(token))
    )
    if row is None or row.revoked_at is not None:
        return None
    if scope is not None and row.scope != scope.value:
        return None
    return row


def touch(session: Session, connection: AgentConnection) -> None:
    connection.last_used_at = utcnow()
    session.flush()


def revoke(session: Session, connection_id: str) -> AgentConnection:
    row = session.get(AgentConnection, connection_id)
    if row is None:
        raise LookupError(f"unknown connection {connection_id}")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.flush()
    return row


def listing(session: Session, limit: int = 100) -> list[AgentConnection]:
    return list(
        session.scalars(
            select(AgentConnection).order_by(desc(AgentConnection.created_at)).limit(limit)
        ).all()
    )


def as_document(connection: AgentConnection, *, token: str | None = None) -> dict[str, Any]:
    """What a client is told about a connection.

    The token appears only when it was just minted. Everything else is safe to show repeatedly.
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    document: dict[str, Any] = {
        "id": connection.id,
        "label": connection.label,
        "scope": connection.scope,
        "agent_id": connection.agent_id,
        "whatsapp": mask(connection.whatsapp_e164),
        "token_prefix": connection.token_prefix,
        "notify_completed": connection.notify_completed,
        "notify_refused": connection.notify_refused,
        "revoked": connection.revoked_at is not None,
        "created_at": connection.created_at.isoformat(),
        "last_used_at": (
            connection.last_used_at.isoformat() if connection.last_used_at else None
        ),
        "endpoints": _endpoints(connection, base),
        "header": "X-Dwarpal-Connection",
    }
    if token is not None:
        document["token"] = token
        document["token_shown_once"] = True
    return document


def _endpoints(connection: AgentConnection, base: str) -> dict[str, str]:
    if connection.scope == ConnectionScope.MERCHANT.value:
        return {
            "overview": f"{base}/merchant/overview",
            "verdicts": f"{base}/merchant/verdicts",
            "mandates": f"{base}/merchant/mandates",
            "agents": f"{base}/merchant/agents",
            "evidence": f"{base}/merchant/evidence",
        }
    return {
        "discovery": f"{base}/.well-known/ap2-merchant",
        "browse": f"{base}/catalog/items",
        "search": f"{base}/catalog/search",
        "policy_terms": f"{base}/policy/terms",
        "quote": f"{base}/checkout/quote",
        "complete": f"{base}/checkout/complete",
    }
