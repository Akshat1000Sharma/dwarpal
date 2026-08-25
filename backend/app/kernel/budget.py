"""Budget accounting under concurrency.

A single open mandate carries a spending cap that several agent sessions may draw against at the
same time. Two concurrent authorisations must never both succeed if together they exceed the cap.

The mechanism is reserve before commitment, with the mandate row taken under a real row-level
lock: reserve atomically, confirm on capture, release on failure, expire on timeout so an
abandoned attempt does not consume budget forever. An application-level read-then-write is a
defect here even if it happens to pass a test, so every path below acquires the lock first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import BudgetReservation, OpenMandate, ReservationStatus
from app.settings import settings


@dataclass(frozen=True)
class BudgetState:
    cap_minor: int | None
    committed_minor: int
    reserved_minor: int

    @property
    def available_minor(self) -> int | None:
        if self.cap_minor is None:
            return None
        return self.cap_minor - self.committed_minor - self.reserved_minor


class BudgetExceeded(Exception):
    def __init__(self, state: BudgetState, requested_minor: int) -> None:
        super().__init__("budget cap would be exceeded")
        self.state = state
        self.requested_minor = requested_minor


def _lock_mandate(session: Session, mandate_id: str) -> OpenMandate | None:
    """Take the mandate row with FOR UPDATE and read it fresh. Everything else runs under it.

    populate_existing is the whole point of this function and must not be dropped. Without it the
    FOR UPDATE row is fetched and then thrown away: if this session already loaded the mandate,
    the identity map hands back the copy from before the lock, so the balance is read from a
    snapshot taken before every other session's committed spend. The lock serialises correctly and
    the arithmetic is still wrong, which is the failure mode this whole module exists to prevent.
    """
    return session.scalar(
        select(OpenMandate)
        .where(OpenMandate.id == mandate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _expire_stale(session: Session, mandate_id: str) -> int:
    now = utcnow()
    result = session.execute(
        update(BudgetReservation)
        .where(
            BudgetReservation.mandate_id == mandate_id,
            BudgetReservation.status == ReservationStatus.RESERVED,
            BudgetReservation.expires_at <= now,
        )
        .values(status=ReservationStatus.EXPIRED, settled_at=now)
    )
    return int(result.rowcount or 0)


def _reserved_total(session: Session, mandate_id: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                BudgetReservation.mandate_id == mandate_id,
                BudgetReservation.status == ReservationStatus.RESERVED,
            )
        )
        or 0
    )


def state(session: Session, mandate_id: str) -> BudgetState:
    """Read-only view. Refreshed for the same reason the locked read is."""
    mandate = session.get(OpenMandate, mandate_id, populate_existing=True)
    if mandate is None:
        raise LookupError(f"unknown mandate {mandate_id}")
    return BudgetState(
        cap_minor=mandate.cap_minor,
        committed_minor=mandate.committed_minor,
        reserved_minor=_reserved_total(session, mandate_id),
    )


def reserve(
    session: Session,
    mandate_id: str,
    amount_minor: int,
    correlation_id: str,
    *,
    ttl_seconds: int | None = None,
) -> BudgetReservation:
    """Atomically reserve against the cap, or raise BudgetExceeded.

    The mandate row is locked before the reserved total is read, so a concurrent caller blocks
    until this transaction commits and then sees this reservation.
    """
    if amount_minor <= 0:
        raise ValueError("reservation amount must be positive")

    mandate = _lock_mandate(session, mandate_id)
    if mandate is None:
        raise LookupError(f"unknown mandate {mandate_id}")

    _expire_stale(session, mandate_id)
    current = BudgetState(
        cap_minor=mandate.cap_minor,
        committed_minor=mandate.committed_minor,
        reserved_minor=_reserved_total(session, mandate_id),
    )
    available = current.available_minor
    if available is not None and amount_minor > available:
        raise BudgetExceeded(current, amount_minor)

    ttl = ttl_seconds if ttl_seconds is not None else settings.BUDGET_RESERVATION_TTL_SECONDS
    reservation = BudgetReservation(
        mandate_id=mandate_id,
        correlation_id=correlation_id,
        amount_minor=amount_minor,
        status=ReservationStatus.RESERVED,
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    session.add(reservation)
    session.flush()
    return reservation


def commit(session: Session, reservation_id: str) -> BudgetReservation:
    """Confirm a reservation once money has actually moved."""
    mandate_id = session.scalar(
        select(BudgetReservation.mandate_id).where(BudgetReservation.id == reservation_id)
    )
    if mandate_id is None:
        raise LookupError(f"unknown reservation {reservation_id}")
    mandate = _lock_mandate(session, mandate_id)
    if mandate is None:
        raise LookupError(f"unknown mandate {mandate_id}")

    reservation = session.get(BudgetReservation, reservation_id)
    assert reservation is not None
    if reservation.status == ReservationStatus.COMMITTED:
        return reservation
    if reservation.status != ReservationStatus.RESERVED:
        raise ValueError(f"reservation {reservation_id} is {reservation.status}, cannot commit")

    reservation.status = ReservationStatus.COMMITTED
    reservation.settled_at = utcnow()
    mandate.committed_minor += reservation.amount_minor
    mandate.use_count += 1
    session.flush()
    return reservation


def release(session: Session, reservation_id: str) -> BudgetReservation:
    """Give the budget back after a failure or an abandoned attempt."""
    reservation = session.get(BudgetReservation, reservation_id)
    if reservation is None:
        raise LookupError(f"unknown reservation {reservation_id}")
    if reservation.status in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
        return reservation
    if reservation.status == ReservationStatus.COMMITTED:
        raise ValueError("a committed reservation cannot be released; issue a refund instead")

    mandate = _lock_mandate(session, reservation.mandate_id)
    if mandate is None:
        raise LookupError(f"unknown mandate {reservation.mandate_id}")
    reservation.status = ReservationStatus.RELEASED
    reservation.settled_at = utcnow()
    session.flush()
    return reservation


def release_by_correlation(session: Session, correlation_id: str) -> int:
    reservations = list(
        session.scalars(
            select(BudgetReservation).where(
                BudgetReservation.correlation_id == correlation_id,
                BudgetReservation.status == ReservationStatus.RESERVED,
            )
        ).all()
    )
    for reservation in reservations:
        release(session, reservation.id)
    return len(reservations)


def sweep_expired(session: Session) -> int:
    """Expire every reservation past its deadline, across all mandates."""
    now = utcnow()
    result = session.execute(
        update(BudgetReservation)
        .where(
            BudgetReservation.status == ReservationStatus.RESERVED,
            BudgetReservation.expires_at <= now,
        )
        .values(status=ReservationStatus.EXPIRED, settled_at=now)
    )
    return int(result.rowcount or 0)
