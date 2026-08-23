"""Replay store.

A credential digest may be presented once. A replayed credential is refused even when it is
otherwise perfectly valid, so the store is written under the database's uniqueness guarantee
rather than a read-then-write that two concurrent presentations could both pass.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import CredentialNonce


class ReplayDetected(Exception):
    def __init__(self, digest: str) -> None:
        super().__init__(f"credential {digest[:16]}... has already been presented")
        self.digest = digest


def remember(
    session: Session, *, digest: str, kind: str, agent_id: str, correlation_id: str
) -> None:
    """Record a first presentation, or raise ReplayDetected.

    The insert is attempted inside a savepoint so a collision does not poison the outer
    transaction, and the primary key is what actually decides the race.
    """
    try:
        with session.begin_nested():
            session.add(
                CredentialNonce(
                    digest=digest,
                    kind=kind,
                    agent_id=agent_id,
                    correlation_id=correlation_id,
                    seen_at=utcnow(),
                )
            )
            session.flush()
    except IntegrityError as exc:
        raise ReplayDetected(digest) from exc


def seen(session: Session, digest: str) -> bool:
    return session.get(CredentialNonce, digest) is not None


def prune(session: Session, older_than_seconds: int) -> int:
    """Drop entries past any credential's possible validity, to bound the table."""
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    result = session.execute(delete(CredentialNonce).where(CredentialNonce.seen_at < cutoff))
    return int(result.rowcount or 0)
