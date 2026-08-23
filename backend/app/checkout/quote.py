"""Quote and merchant-signed Checkout.

The merchant signs its Checkout record first, and that signature is a commitment to fulfil at the
stated SKU, price and shipping. Overselling, price drift between quote and payment, and stale cart
state are prevented here: stock is held under a row lock, prices are frozen into a snapshot, and
the signed Checkout is what every later step is compared against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.ap2.jose import sha256_b64url, sign_jws
from app.ap2.models import Checkout, Item, LineItem, Merchant, Total
from app.ap2.schema_validation import assert_conforms
from app.ap2.vocabulary import CHECKOUT_JWT_TYP, CheckoutStatus, TotalType
from app.catalog import policy_terms
from app.catalog import service as catalog
from app.db.base import utcnow
from app.db.models import CheckoutSession, CheckoutState
from app.escalation.service import cart_fingerprint
from app.kernel import inventory
from app.kernel.reasons import ReasonCode
from app.keys import merchant_key
from app.settings import settings

QUOTE_TTL_SECONDS = 600


class QuoteError(Exception):
    """A quote could not be produced, with the reason code an agent can act on."""

    def __init__(self, reason_code: ReasonCode, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class RequestedLine:
    sku: str
    quantity: int


@dataclass
class QuoteResult:
    row: CheckoutSession
    checkout: Checkout
    checkout_jwt: str
    checkout_hash: str
    policy_hash: str
    snapshot: list[dict[str, Any]]


def merchant_identity() -> Merchant:
    return Merchant(
        id=settings.MERCHANT_ID, name=settings.MERCHANT_NAME, website=settings.MERCHANT_WEBSITE
    )


def create_quote(
    session: Session,
    *,
    agent_id: str,
    correlation_id: str,
    lines: list[RequestedLine],
    verified: bool = False,
    ttl_seconds: int = QUOTE_TTL_SECONDS,
) -> QuoteResult:
    """Freeze prices, hold stock, and sign the Checkout the merchant commits to."""
    if not lines:
        raise QuoteError(ReasonCode.ITEM_UNKNOWN, "a quote must contain at least one line")

    terms = policy_terms.active_terms(session)
    checkout_id = f"co_{sha256_b64url(f'{correlation_id}:{agent_id}'.encode())[:22]}"

    line_items: list[LineItem] = []
    snapshot: list[dict[str, Any]] = []
    subtotal = 0
    currency = "INR"

    for index, requested in enumerate(lines):
        entry = catalog.by_sku(session, requested.sku)
        if entry is None:
            raise QuoteError(
                ReasonCode.ITEM_UNKNOWN,
                f"unknown sku {requested.sku}",
                sku=requested.sku,
            )
        product = entry.product
        currency = product.currency

        try:
            inventory.place_hold(
                session,
                sku=requested.sku,
                quantity=requested.quantity,
                agent_id=agent_id,
                checkout_id=checkout_id,
                correlation_id=correlation_id,
            )
        except inventory.QuantityOutOfRange as exc:
            raise QuoteError(
                ReasonCode.QUANTITY_OUT_OF_RANGE,
                str(exc),
                sku=exc.sku,
                min_order_quantity=exc.minimum,
                max_order_quantity=exc.maximum,
            ) from exc
        except inventory.HoldQuotaExceeded as exc:
            raise QuoteError(
                ReasonCode.HOLD_QUOTA_EXCEEDED,
                "this agent already holds its maximum number of carts",
                active_holds=exc.active_holds,
                quota=exc.quota,
            ) from exc
        except inventory.InventoryUnavailable as exc:
            substitute = catalog.substitute_for(session, requested.sku)
            raise QuoteError(
                ReasonCode.INVENTORY_UNAVAILABLE,
                f"{requested.sku} is no longer available in the quantity requested",
                sku=exc.sku,
                requested=exc.requested,
                available=exc.available,
                substitute=substitute.as_document() if substitute else None,
            ) from exc

        line_total = product.price_minor * requested.quantity
        subtotal += line_total
        line_items.append(
            LineItem(
                id=f"li_{index + 1}",
                item=Item(id=product.sku, title=product.title, price=product.price_minor),
                quantity=requested.quantity,
                totals=[Total(type=TotalType.TOTAL, amount=line_total)],
            )
        )
        snapshot.append(entry.snapshot())

    total = subtotal
    checkout = Checkout(
        id=checkout_id,
        merchant=merchant_identity(),
        line_items=line_items,
        status=CheckoutStatus.READY_FOR_COMPLETE,
        currency=currency,
        totals=[
            Total(type=TotalType.SUBTOTAL, amount=subtotal, display_text="Subtotal"),
            Total(type=TotalType.TOTAL, amount=total, display_text="Total"),
        ],
        links=[
            {"type": "terms", "url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/policy/terms"},
            {
                "type": "complete",
                "url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/checkout/complete",
            },
        ],
        expires_at=(utcnow() + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    checkout_document = checkout.model_dump(mode="json", exclude_none=True)
    assert_conforms("checkout", checkout_document)

    checkout_jwt = sign_jws(
        {
            "iss": settings.MERCHANT_ID,
            "iat": int(utcnow().timestamp()),
            "checkout_id": checkout_id,
            "policy_hash": terms.content_hash,
            "checkout": checkout_document,
            "expires_at": checkout.expires_at,
        },
        merchant_key(),
        typ=CHECKOUT_JWT_TYP,
    )
    checkout_hash = sha256_b64url(checkout_jwt.encode("ascii"))
    fingerprint = cart_fingerprint(
        checkout=checkout_document, total_minor=total, policy_hash=terms.content_hash
    )

    row = CheckoutSession(
        id=checkout_id,
        correlation_id=correlation_id,
        agent_id=agent_id,
        state=CheckoutState.SIGNED,
        currency=currency,
        total_minor=total,
        policy_hash=terms.content_hash,
        checkout=checkout_document,
        checkout_jwt=checkout_jwt,
        checkout_hash=checkout_hash,
        catalog_snapshot=snapshot,
        cart_fingerprint=fingerprint,
        verified=verified,
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    session.flush()

    return QuoteResult(
        row=row,
        checkout=checkout,
        checkout_jwt=checkout_jwt,
        checkout_hash=checkout_hash,
        policy_hash=terms.content_hash,
        snapshot=snapshot,
    )


def quote_document(result: QuoteResult) -> dict[str, Any]:
    """What the agent receives, including everything it needs to build its closed mandate."""
    return {
        "checkout_id": result.row.id,
        "checkout": result.row.checkout,
        "checkout_jwt": result.checkout_jwt,
        "checkout_hash": result.checkout_hash,
        "policy_hash": result.policy_hash,
        "total": {"amount": result.row.total_minor, "currency": result.row.currency},
        "expires_at": result.row.expires_at.isoformat(),
        "next": {
            "closed_checkout_mandate": {
                "vct": "mandate.checkout.1",
                "checkout_jwt": "the value above, verbatim",
                "checkout_hash": "the value above, verbatim",
            },
            "closed_payment_mandate": {
                "vct": "mandate.payment.1",
                "transaction_id": result.checkout_hash,
                "payment_amount": {
                    "amount": result.row.total_minor,
                    "currency": result.row.currency,
                },
            },
        },
    }
