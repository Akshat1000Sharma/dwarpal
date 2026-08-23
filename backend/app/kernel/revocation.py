"""Mandate revocation, including revocation that lands after capture.

Revocation is checked immediately before execution, not at the start of a flow. The hard case is
a revocation that arrives after Razorpay has already captured: the system must detect it, issue a
compensating refund automatically, record the outcome under its own status, and still file the
evidence packet. The refund itself lives in the payments layer, which the kernel must not import,
so this module reports the situation and the orchestrator acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import OpenMandate


@dataclass(frozen=True)
class RevocationState:
    revoked: bool
    revoked_at: datetime | None = None
    reason: str | None = None

    def as_evidence(self) -> dict[str, object]:
        return {
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "reason": self.reason,
        }


def revoke(session: Session, mandate_id: str, reason: str = "revoked by principal") -> OpenMandate:
    mandate = session.scalar(
        select(OpenMandate).where(OpenMandate.id == mandate_id).with_for_update()
    )
    if mandate is None:
        raise LookupError(f"unknown mandate {mandate_id}")
    if mandate.revoked_at is None:
        mandate.revoked_at = utcnow()
        mandate.revoked_reason = reason
        session.flush()
    return mandate


def revoke_by_digest(
    session: Session, digest: str, reason: str = "revoked by principal"
) -> OpenMandate:
    mandate = session.scalar(
        select(OpenMandate).where(OpenMandate.digest == digest).with_for_update()
    )
    if mandate is None:
        raise LookupError(f"unknown mandate digest {digest}")
    return revoke(session, mandate.id, reason)


def check(session: Session, mandate_id: str | None) -> RevocationState:
    """Read revocation state at the latest possible moment before money moves."""
    if mandate_id is None:
        return RevocationState(revoked=False)
    mandate = session.get(OpenMandate, mandate_id)
    if mandate is None:
        return RevocationState(revoked=False)
    session.refresh(mandate)
    if mandate.revoked_at is None:
        return RevocationState(revoked=False)
    return RevocationState(True, mandate.revoked_at, mandate.revoked_reason)


def revoked_after(state: RevocationState, moment: datetime) -> bool:
    """True when the revocation landed after the given moment, typically capture."""
    return bool(state.revoked and state.revoked_at and state.revoked_at > moment)
