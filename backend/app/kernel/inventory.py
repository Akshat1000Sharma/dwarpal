"""Inventory holds, hold quotas and denial-of-inventory defence.

Adding to a cart places a hold on stock. Holds expire. A single agent must not be able to exhaust
the merchant's inventory by placing holds it never converts, so holds are quota-limited per agent.

When two agents contend for the last unit, one wins and the other receives a structured answer
saying the item is no longer available, with a substitute where one exists. It never receives a
server error, because an agent cannot act on a 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import HoldStatus, InventoryHold, Product
from app.settings import settings


@dataclass(frozen=True)
class HoldRequest:
    sku: str
    quantity: int


class InventoryUnavailable(Exception):
    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(f"{sku}: requested {requested}, available {available}")
        self.sku = sku
        self.requested = requested
        self.available = available


class HoldQuotaExceeded(Exception):
    def __init__(self, agent_id: str, active_holds: int, quota: int) -> None:
        super().__init__(f"{agent_id} holds {active_holds} of {quota} permitted")
        self.agent_id = agent_id
        self.active_holds = active_holds
        self.quota = quota


class QuantityOutOfRange(Exception):
    def __init__(self, sku: str, quantity: int, minimum: int, maximum: int) -> None:
        super().__init__(f"{sku}: {quantity} outside [{minimum}, {maximum}]")
        self.sku = sku
        self.quantity = quantity
        self.minimum = minimum
        self.maximum = maximum


def expire_stale(session: Session) -> int:
    now = utcnow()
    result = session.execute(
        update(InventoryHold)
        .where(InventoryHold.status == HoldStatus.HELD, InventoryHold.expires_at <= now)
        .values(status=HoldStatus.EXPIRED)
    )
    return int(result.rowcount or 0)


def _active_hold_units(session: Session, product_id: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(InventoryHold.quantity), 0)).where(
                InventoryHold.product_id == product_id,
                InventoryHold.status == HoldStatus.HELD,
                InventoryHold.expires_at > utcnow(),
            )
        )
        or 0
    )


def agent_active_holds(session: Session, agent_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(InventoryHold.id)).where(
                InventoryHold.agent_id == agent_id,
                InventoryHold.status == HoldStatus.HELD,
                InventoryHold.expires_at > utcnow(),
            )
        )
        or 0
    )


def available(session: Session, product: Product) -> int:
    return max(0, product.stock_total - _active_hold_units(session, product.id))


def place_hold(
    session: Session,
    *,
    sku: str,
    quantity: int,
    agent_id: str,
    checkout_id: str,
    correlation_id: str,
    ttl_seconds: int | None = None,
    quota: int | None = None,
) -> InventoryHold:
    """Hold stock under a row lock on the product.

    The product row is locked before availability is computed, so two agents racing for the last
    unit serialise: the first commits its hold, the second recomputes and sees zero available.
    """
    product = session.scalar(select(Product).where(Product.sku == sku).with_for_update())
    if product is None:
        raise LookupError(f"unknown sku {sku}")

    if quantity < product.min_order_quantity or quantity > product.max_order_quantity:
        raise QuantityOutOfRange(
            sku, quantity, product.min_order_quantity, product.max_order_quantity
        )

    limit = quota if quota is not None else settings.INVENTORY_HOLD_QUOTA_PER_AGENT
    active = agent_active_holds(session, agent_id)
    if active >= limit:
        raise HoldQuotaExceeded(agent_id, active, limit)

    expire_stale(session)
    free = product.stock_total - _active_hold_units(session, product.id)
    if quantity > free:
        raise InventoryUnavailable(sku, quantity, max(0, free))

    ttl = ttl_seconds if ttl_seconds is not None else settings.INVENTORY_HOLD_TTL_SECONDS
    hold = InventoryHold(
        product_id=product.id,
        agent_id=agent_id,
        checkout_id=checkout_id,
        correlation_id=correlation_id,
        quantity=quantity,
        status=HoldStatus.HELD,
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    session.add(hold)
    session.flush()
    return hold


def consume(session: Session, checkout_id: str) -> int:
    """Convert holds into a sale, decrementing real stock under a lock per product."""
    holds = list(
        session.scalars(
            select(InventoryHold).where(
                InventoryHold.checkout_id == checkout_id, InventoryHold.status == HoldStatus.HELD
            )
        ).all()
    )
    for hold in holds:
        product = session.scalar(
            select(Product).where(Product.id == hold.product_id).with_for_update()
        )
        if product is None:
            continue
        product.stock_total = max(0, product.stock_total - hold.quantity)
        hold.status = HoldStatus.CONSUMED
    session.flush()
    return len(holds)


def held_units(session: Session, checkout_id: str, *, now: datetime | None = None) -> int:
    """Units this Checkout still has held. Zero means the sale would not be backed by stock.

    A hold past its TTL is not counted even though nothing has flipped its status yet: expiry is
    what the clock says, not what a later writer gets round to recording.
    """
    moment = now or utcnow()
    total = session.scalar(
        select(func.coalesce(func.sum(InventoryHold.quantity), 0)).where(
            InventoryHold.checkout_id == checkout_id,
            InventoryHold.status == HoldStatus.HELD,
            InventoryHold.expires_at > moment,
        )
    )
    return int(total or 0)


def extend(session: Session, checkout_id: str, until: datetime) -> int:
    """Hold this Checkout's stock at least until the given moment.

    Used when a question goes to a human: the cart must still be there when they answer, and the
    quote TTL is shorter than the deadline they were given.
    """
    result = session.execute(
        update(InventoryHold)
        .where(
            InventoryHold.checkout_id == checkout_id,
            InventoryHold.status == HoldStatus.HELD,
            InventoryHold.expires_at < until,
        )
        .values(expires_at=until)
    )
    return int(result.rowcount or 0)


def release(session: Session, checkout_id: str) -> int:
    result = session.execute(
        update(InventoryHold)
        .where(InventoryHold.checkout_id == checkout_id, InventoryHold.status == HoldStatus.HELD)
        .values(status=HoldStatus.RELEASED)
    )
    return int(result.rowcount or 0)


def restore(session: Session, checkout_id: str) -> int:
    """Put stock back after a compensating refund."""
    holds = list(
        session.scalars(
            select(InventoryHold).where(
                InventoryHold.checkout_id == checkout_id,
                InventoryHold.status == HoldStatus.CONSUMED,
            )
        ).all()
    )
    for hold in holds:
        product = session.scalar(
            select(Product).where(Product.id == hold.product_id).with_for_update()
        )
        if product is not None:
            product.stock_total += hold.quantity
        hold.status = HoldStatus.RELEASED
    session.flush()
    return len(holds)
