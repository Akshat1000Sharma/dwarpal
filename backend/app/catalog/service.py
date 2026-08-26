"""Agent-readable catalog.

What distinguishes this from a product JSON dump is the constraint layer: every item carries
machine-readable purchase constraints, and availability is a live count net of active holds
rather than a static in-stock flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import HoldStatus, InventoryHold, Product

# Imported rather than redefined: the catalog only advertises what the kernel actually
# enforces, so the published flag cannot drift away from the rule behind it.
from app.kernel.kernel import RESTRICTED_CATEGORIES


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
            "image": {"url": p.image_url, "alt": p.image_alt or p.title} if p.image_url else None,
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
            "image_url": self.product.image_url,
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


# Words that carry no information about which product is wanted. An agent asking for "one pack of
# Nilgiri black tea" is asking for tea, and a merchant that answers only when the phrasing happens
# to be a substring of its own title is not readable by a machine that speaks English.
_NOISE_WORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "box", "buy", "can", "find", "for", "from",
        "get", "give", "has", "have", "i", "in", "is", "it", "just", "looking", "me", "my", "need",
        "of", "on", "one", "or", "pack", "packet", "please", "some", "search", "the", "to", "two",
        "unit", "units", "want", "with", "would", "you", "your",
    }
)


def _terms(query: str) -> list[str]:
    """The words worth matching on, longest first so the most specific one decides the order."""
    words = re.split(r"[^0-9A-Za-z]+", query.strip().lower())
    meaningful = [w for w in words if len(w) > 1 and w not in _NOISE_WORDS]
    return sorted(dict.fromkeys(meaningful), key=len, reverse=True)


_MAX_TERMS = 12


def search(session: Session, query: str, *, limit: int = 25) -> list[CatalogEntry]:
    """Find items matching a query, which may be a phrase rather than a keyword.

    An item matches when every meaningful word in the query appears somewhere in its text. That is
    deliberately stricter than matching any word: an agent asking for "black tea" wants the tea,
    not everything black and everything tea-like. A query with no meaningful words lists the
    catalog, because an agent that has lost its way needs to see what is actually for sale.
    """
    # One predicate is built per term against four columns, and the query is agent-supplied, so
    # the term count is bounded rather than left to whatever arrives.
    terms = _terms(query)[:_MAX_TERMS]
    statement = select(Product).order_by(Product.sku).limit(limit)
    if terms:
        statement = select(Product).where(
            *[
                or_(
                    Product.title.ilike(f"%{term}%"),
                    Product.description.ilike(f"%{term}%"),
                    Product.sku.ilike(f"%{term}%"),
                    Product.category.ilike(f"%{term}%"),
                )
                for term in terms
            ]
        ).order_by(Product.sku).limit(limit)
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
