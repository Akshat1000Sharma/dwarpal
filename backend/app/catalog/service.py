"""Agent-readable catalog.

What distinguishes this from a product JSON dump is the constraint layer: every item carries
machine-readable purchase constraints, and availability is a live count net of active holds
rather than a static in-stock flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import HoldStatus, InventoryHold, Product

RESTRICTED_CATEGORIES: frozenset[str] = frozenset({"alcohol", "restricted-blades"})


@dataclass(frozen=True)
class CatalogEntry:
    product: Product
    available: int

    def as_document(self) -> dict[str, Any]:
        p = self.product
        return {
            "sku": p.sku,
            "title": p.title,
            "description": p.description,
            "category": p.category,
            "price": {"amount": p.price_minor, "currency": p.currency},
            "availability": {
                "in_stock": self.available > 0,
                "available_quantity": self.available,
                "stock_total": p.stock_total,
            },
            "purchase_constraints": {
                "min_order_quantity": p.min_order_quantity,
                "max_order_quantity": p.max_order_quantity,
                "returnable": p.returnable,
                "return_window_days": p.return_window_days,
                "age_restricted": p.age_restricted,
                "region_locked": list(p.region_lock or []),
                "restricted_category": p.category in RESTRICTED_CATEGORIES,
                "perishable": p.perishable,
            },
            "attributes": dict(p.attributes or {}),
            "updated_at": p.updated_at.isoformat(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Frozen record of what the buyer was shown, stored inside the evidence packet."""
        return {
            "sku": self.product.sku,
            "title": self.product.title,
            "category": self.product.category,
            "price_minor": self.product.price_minor,
            "currency": self.product.currency,
            "available_at_quote": self.available,
            "purchase_constraints": self.as_document()["purchase_constraints"],
            "observed_at": utcnow().isoformat(),
        }


def held_quantities(session: Session, product_ids: list[str]) -> dict[str, int]:
    """Units currently held by unexpired holds, per product."""
    if not product_ids:
        return {}
    rows = session.execute(
        select(InventoryHold.product_id, func.coalesce(func.sum(InventoryHold.quantity), 0))
        .where(
            InventoryHold.product_id.in_(product_ids),
            InventoryHold.status == HoldStatus.HELD,
            InventoryHold.expires_at > utcnow(),
        )
        .group_by(InventoryHold.product_id)
    ).all()
    return {product_id: int(total) for product_id, total in rows}


def _entries(session: Session, products: list[Product]) -> list[CatalogEntry]:
    held = held_quantities(session, [p.id for p in products])
    return [
        CatalogEntry(product=p, available=max(0, p.stock_total - held.get(p.id, 0)))
        for p in products
    ]


def browse(
    session: Session,
    *,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CatalogEntry]:
    statement = select(Product).order_by(Product.sku)
    if category:
        statement = statement.where(Product.category == category)
    products = list(session.scalars(statement.limit(limit).offset(offset)).all())
    return _entries(session, products)


def search(session: Session, query: str, *, limit: int = 25) -> list[CatalogEntry]:
    pattern = f"%{query.strip()}%"
    statement = (
        select(Product)
        .where(
            or_(
                Product.title.ilike(pattern),
                Product.description.ilike(pattern),
                Product.sku.ilike(pattern),
                Product.category.ilike(pattern),
            )
        )
        .order_by(Product.sku)
        .limit(limit)
    )
    return _entries(session, list(session.scalars(statement).all()))


def by_sku(session: Session, sku: str) -> CatalogEntry | None:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        return None
    return _entries(session, [product])[0]


def categories(session: Session) -> list[str]:
    return sorted(session.scalars(select(Product.category).distinct()).all())


def substitute_for(session: Session, sku: str) -> CatalogEntry | None:
    """A same-category alternative with stock, offered when an item becomes unavailable."""
    original = session.scalar(select(Product).where(Product.sku == sku))
    if original is None:
        return None
    candidates = list(
        session.scalars(
            select(Product)
            .where(Product.category == original.category, Product.sku != original.sku)
            .order_by(func.abs(Product.price_minor - original.price_minor))
            .limit(5)
        ).all()
    )
    for entry in _entries(session, candidates):
        if entry.available > 0:
            return entry
    return None
