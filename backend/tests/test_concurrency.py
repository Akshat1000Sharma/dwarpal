"""Concurrency guarantees, fuzzed against real PostgreSQL row locking.

Every test here includes a naive control: the same assertions run against an application-level
read-then-write, and that control must fail. A test that passes both before and after the fix
proves nothing, so each case asserts the naive path actually breaches the guarantee while the real
implementation holds.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import base as db_base
from app.db.base import utcnow
from app.db.models import (
    BudgetReservation,
    HoldStatus,
    InventoryHold,
    OpenMandate,
    Product,
    ReservationStatus,
)
from app.kernel import budget, inventory

pytestmark = pytest.mark.concurrency

CAP_MINOR = 100_000
DRAW_MINOR = 10_000
THREADS = 30


def _make_mandate(session, cap_minor: int = CAP_MINOR) -> str:
    mandate = OpenMandate(
        kind="checkout",
        digest=f"digest-{utcnow().timestamp()}",
        sd_jwt="",
        claims={},
        agent_id="agent:fuzz",
        key_thumbprint="tp",
        issuer_id="did:web:trusted-surface.dwarpal.test",
        tier="accredited",
        cap_minor=cap_minor,
        currency="INR",
    )
    session.add(mandate)
    session.commit()
    return mandate.id


def _reserved_and_committed(session, mandate_id: str) -> int:
    reserved = int(
        session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                BudgetReservation.mandate_id == mandate_id,
                BudgetReservation.status.in_(
                    [ReservationStatus.RESERVED, ReservationStatus.COMMITTED]
                ),
            )
        )
        or 0
    )
    return reserved


def _naive_reserve(mandate_id: str, amount_minor: int) -> bool:
    """The control: read the totals, decide, then write. No lock is taken.

    This is what a plausible implementation looks like, and it is a defect. It exists only in this
    test module so the fuzz can demonstrate the difference.
    """
    session = db_base.SessionFactory()
    try:
        mandate = session.get(OpenMandate, mandate_id)
        assert mandate is not None
        used = int(
            session.scalar(
                select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                    BudgetReservation.mandate_id == mandate_id,
                    BudgetReservation.status == ReservationStatus.RESERVED,
                )
            )
            or 0
        )
        if used + amount_minor > (mandate.cap_minor or 0):
            return False
        # Widen the window the same way real work would.
        threading.Event().wait(0.01)
        session.add(
            BudgetReservation(
                mandate_id=mandate_id,
                correlation_id="naive",
                amount_minor=amount_minor,
                status=ReservationStatus.RESERVED,
                expires_at=utcnow() + timedelta(seconds=300),
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _locked_reserve(mandate_id: str, amount_minor: int) -> bool:
    """The real implementation, which takes the mandate row FOR UPDATE."""
    session = db_base.SessionFactory()
    try:
        budget.reserve(session, mandate_id, amount_minor, "fuzz")
        session.commit()
        return True
    except budget.BudgetExceeded:
        session.rollback()
        return False
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _run_concurrently(fn, mandate_id: str, threads: int = THREADS) -> int:
    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(lambda _: fn(mandate_id, DRAW_MINOR), range(threads)))
    return sum(1 for granted in results if granted)


def test_concurrent_draws_never_breach_the_cap(db):
    """Many sessions drawing against one mandate must never exceed its cap."""
    mandate_id = _make_mandate(db)
    granted = _run_concurrently(_locked_reserve, mandate_id)

    total = _reserved_and_committed(db, mandate_id)
    assert total <= CAP_MINOR, f"cap breached: {total} > {CAP_MINOR}"
    assert granted == CAP_MINOR // DRAW_MINOR
    assert total == CAP_MINOR


def test_the_naive_implementation_demonstrably_breaches_the_cap(db):
    """The control. If this ever passes, the fuzz above is not testing anything."""
    mandate_id = _make_mandate(db)
    _run_concurrently(_naive_reserve, mandate_id)

    total = _reserved_and_committed(db, mandate_id)
    assert total > CAP_MINOR, (
        "the naive check-then-write did not breach the cap under contention, so this fuzz is not "
        "exercising the race it claims to"
    )


def _draw_like_a_checkout(mandate_id: str, amount_minor: int) -> bool:
    """Reserve the way a real checkout does: with the mandate already loaded in this session.

    This is the distinction that matters. `_locked_reserve` starts from an empty session, so the
    FOR UPDATE read is the first time that row is seen and is necessarily fresh. A real checkout
    has already loaded the mandate before it reaches the kernel, because it had to look it up to
    know which mandate this credential belongs to. If the locked read is then allowed to return
    the copy already in the session, the lock serialises correctly and the balance is still read
    from before every other session's spend.
    """
    session = db_base.SessionFactory()
    try:
        preloaded = session.get(OpenMandate, mandate_id)
        assert preloaded is not None
        # Verification, the kernel's earlier steps and the gateway call all happen between the
        # lookup and the reservation in a real checkout. The wait stands in for them, so the
        # window this test needs is the window the application actually has.
        threading.Event().wait(0.01)
        reservation = budget.reserve(session, mandate_id, amount_minor, "checkout-like")
        # Settle in the same transaction, which is what an inline capture does. This is what turns
        # a stale read into a breach: the spend moves from the reservation table, which is always
        # read fresh, onto the mandate row, which is the thing being cached.
        budget.commit(session, reservation.id)
        session.commit()
        return True
    except budget.BudgetExceeded:
        session.rollback()
        return False
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def test_the_cap_holds_when_the_mandate_was_already_loaded(db):
    """Regression: a locked read that returns a cached row is not a locked read.

    Found by the scenario suite, not by the fuzz above, because the fuzz reserved from a fresh
    session every time and a real checkout never does.
    """
    mandate_id = _make_mandate(db)
    granted = _run_concurrently(_draw_like_a_checkout, mandate_id)

    committed = int(
        db.scalar(
            select(func.coalesce(func.sum(OpenMandate.committed_minor), 0)).where(
                OpenMandate.id == mandate_id
            )
        )
        or 0
    )
    assert committed <= CAP_MINOR, f"cap breached: {committed} > {CAP_MINOR}"
    assert granted == CAP_MINOR // DRAW_MINOR, (
        f"expected exactly {CAP_MINOR // DRAW_MINOR} draws to succeed, got {granted}"
    )


def test_the_kernel_takes_its_locked_reads_with_a_refresh(db):
    """Guard the fix itself, so it cannot be quietly optimised away.

    budget._lock_mandate is the only place the mandate balance is read before money is allowed to
    move. The regression test above proves the behaviour; this proves the mechanism is still the
    one that produces it.
    """
    mandate_id = _make_mandate(db)
    other = db_base.SessionFactory()
    try:
        assert other.get(OpenMandate, mandate_id) is not None

        spender = db_base.SessionFactory()
        try:
            row = spender.get(OpenMandate, mandate_id, populate_existing=True)
            assert row is not None
            row.committed_minor = CAP_MINOR
            spender.commit()
        finally:
            spender.close()

        locked = budget._lock_mandate(other, mandate_id)
        assert locked is not None
        assert locked.committed_minor == CAP_MINOR, (
            "budget._lock_mandate returned a stale balance; the FOR UPDATE read must refresh"
        )
    finally:
        other.rollback()
        other.close()


def test_reservations_expire_so_an_abandoned_attempt_frees_budget(db):
    mandate_id = _make_mandate(db)
    reservation = budget.reserve(db, mandate_id, CAP_MINOR, "abandoned", ttl_seconds=-1)
    db.commit()

    assert budget.state(db, mandate_id).available_minor == 0
    swept = budget.sweep_expired(db)
    db.commit()
    assert swept == 1
    assert budget.state(db, mandate_id).available_minor == CAP_MINOR
    assert db.get(BudgetReservation, reservation.id).status == ReservationStatus.EXPIRED


def test_committed_budget_is_not_released_by_a_later_expiry_sweep(db):
    mandate_id = _make_mandate(db)
    reservation = budget.reserve(db, mandate_id, DRAW_MINOR, "committed")
    budget.commit(db, reservation.id)
    db.commit()

    budget.sweep_expired(db)
    db.commit()
    state = budget.state(db, mandate_id)
    assert state.committed_minor == DRAW_MINOR
    assert state.available_minor == CAP_MINOR - DRAW_MINOR


def test_release_returns_budget_but_commit_cannot_be_undone(db):
    mandate_id = _make_mandate(db)
    released = budget.reserve(db, mandate_id, DRAW_MINOR, "released")
    budget.release(db, released.id)
    assert budget.state(db, mandate_id).available_minor == CAP_MINOR

    committed = budget.reserve(db, mandate_id, DRAW_MINOR, "committed")
    budget.commit(db, committed.id)
    with pytest.raises(ValueError):
        budget.release(db, committed.id)


# --- inventory ---------------------------------------------------------------------------------


def _hold(sku: str, agent_id: str) -> str:
    session = db_base.SessionFactory()
    try:
        inventory.place_hold(
            session,
            sku=sku,
            quantity=1,
            agent_id=agent_id,
            checkout_id=f"co-{agent_id}",
            correlation_id="fuzz",
            quota=1000,
        )
        session.commit()
        return "held"
    except inventory.InventoryUnavailable:
        session.rollback()
        return "unavailable"
    except Exception as exc:
        session.rollback()
        return f"error:{type(exc).__name__}"
    finally:
        session.close()


def test_contending_for_the_last_unit_yields_one_winner_and_no_server_errors(seeded):
    db = seeded
    product = db.scalar(select(Product).where(Product.sku == "DWP-PEN-012"))
    assert product.stock_total == 1
    db.commit()

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda i: _hold("DWP-PEN-012", f"agent-{i}"), range(12)))

    assert outcomes.count("held") == 1
    assert outcomes.count("unavailable") == 11
    # The loser must receive a structured refusal, never a server error.
    assert not [o for o in outcomes if o.startswith("error:")], outcomes


def test_holds_are_quota_limited_per_agent(seeded):
    db = seeded
    for index in range(3):
        inventory.place_hold(
            db,
            sku="DWP-NTB-011",
            quantity=1,
            agent_id="agent:greedy",
            checkout_id=f"co-{index}",
            correlation_id="fuzz",
            quota=3,
        )
    db.commit()

    with pytest.raises(inventory.HoldQuotaExceeded):
        inventory.place_hold(
            db,
            sku="DWP-NTB-011",
            quantity=1,
            agent_id="agent:greedy",
            checkout_id="co-overflow",
            correlation_id="fuzz",
            quota=3,
        )

    # A different agent is unaffected by one agent's quota.
    inventory.place_hold(
        db,
        sku="DWP-NTB-011",
        quantity=1,
        agent_id="agent:other",
        checkout_id="co-other",
        correlation_id="fuzz",
        quota=3,
    )


def test_one_agent_cannot_exhaust_inventory_with_holds_it_never_converts(seeded):
    """Denial of inventory: the quota must stop a single agent locking up the shelf."""
    db = seeded
    product = db.scalar(select(Product).where(Product.sku == "DWP-LMP-009"))
    stock = product.stock_total
    db.commit()

    placed = 0
    for index in range(stock + 5):
        try:
            inventory.place_hold(
                db,
                sku="DWP-LMP-009",
                quantity=1,
                agent_id="agent:hoarder",
                checkout_id=f"co-hoard-{index}",
                correlation_id="fuzz",
                quota=2,
            )
            placed += 1
        except inventory.HoldQuotaExceeded:
            break
    db.commit()

    assert placed == 2
    assert inventory.available(db, product) == stock - 2, "stock must remain purchasable by others"


def test_expired_holds_release_stock(seeded):
    db = seeded
    product = db.scalar(select(Product).where(Product.sku == "DWP-LMP-009"))
    stock = product.stock_total
    inventory.place_hold(
        db,
        sku="DWP-LMP-009",
        quantity=1,
        agent_id="agent:slow",
        checkout_id="co-slow",
        correlation_id="fuzz",
        ttl_seconds=-1,
    )
    db.commit()

    inventory.expire_stale(db)
    db.commit()
    assert inventory.available(db, product) == stock
    hold = db.scalar(select(InventoryHold).where(InventoryHold.checkout_id == "co-slow"))
    assert hold.status == HoldStatus.EXPIRED
