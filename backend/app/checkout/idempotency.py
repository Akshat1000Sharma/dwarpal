"""Idempotency for state-changing operations.

An agent retrying after a timeout must never produce a second charge. A key is claimed under the
database's primary-key guarantee, so two concurrent retries cannot both believe they are the
first; the loser replays the stored response instead of acting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ap2.jose import hash_payload
from app.db.base import utcnow
from app.db.models import IdempotencyKey


class IdempotencyConflict(Exception):
    """The same key was reused with a different request body."""

    def __init__(self, key: str) -> None:
        super().__init__(f"idempotency key {key} was already used with a different request")
        self.key = key


@dataclass(frozen=True)
class Replay:
    status_code: int
    response: dict[str, Any]


def request_hash(payload: Any) -> str:
    return hash_payload(payload)


def lookup(session: Session, key: str, payload: Any) -> Replay | None:
    """Return the stored response when this exact request has been seen before."""
    row = session.get(IdempotencyKey, key)
    if row is None:
        return None
    if row.request_hash != request_hash(payload):
        raise IdempotencyConflict(key)
    return Replay(status_code=row.status_code, response=row.response)


def claim(session: Session, key: str, endpoint: str, payload: Any) -> bool:
    """Take the key, or report that someone else already holds it."""
    try:
        with session.begin_nested():
            session.add(
                IdempotencyKey(
                    key=key,
                    endpoint=endpoint,
                    request_hash=request_hash(payload),
                    status_code=0,
                    response={},
                    created_at=utcnow(),
                )
            )
            session.flush()
        return True
    except IntegrityError:
        return False


def record(session: Session, key: str, *, status_code: int, response: dict[str, Any]) -> None:
    row = session.get(IdempotencyKey, key)
    if row is None:
        return
    row.status_code = status_code
    row.response = response
    session.flush()
