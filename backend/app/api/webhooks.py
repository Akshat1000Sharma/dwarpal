"""Inbound webhooks from Razorpay and Meta.

Both verify a signature over the raw request body before anything parses it. An unverified
notification is rejected without being processed at all.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.checkout.complete import finalise_captured, finalise_failed
from app.db.base import utcnow
from app.db.models import (
    CheckoutSession,
    CheckoutState,
    Payment,
    PaymentException,
    PaymentStatus,
    Refund,
    RefundStatus,
)
from app.errors import AgentError
from app.escalation import service as escalation_service
from app.escalation import whatsapp
from app.kernel.reasons import ReasonCode
from app.logging import get_logger
from app.payments.gateway import verify_webhook_signature

logger = get_logger(__name__)
router = APIRouter(tags=["webhooks"])


# Razorpay states in which the buyer's money is actually held.
_LIVE_AT_GATEWAY = frozenset({"authorized", "captured"})
# Local states in which Dwarpal has decided not to settle this checkout.
_NOT_SETTLING = frozenset({CheckoutState.CANCELLED, CheckoutState.REFUSED})
_ORPHANED_MONEY = "gateway_holds_money_for_a_checkout_we_will_not_settle"


def _flag_orphaned_money(db: Session, payment_row: Payment, entity: dict[str, Any]) -> bool:
    """File the case where the gateway holds money for a checkout Dwarpal has already closed.

    Refusing to settle is correct and happens upstream of this. What must not happen is refusing
    silently: the buyer's money is held against an order that will never be fulfilled, and nothing
    reconciles it back. Razorpay is authoritative, so the disagreement is recorded, not corrected.
    """
    if str(entity.get("status")) not in _LIVE_AT_GATEWAY:
        return False
    checkout = db.get(CheckoutSession, payment_row.checkout_id)
    if checkout is None or checkout.state not in _NOT_SETTLING:
        return False
    seen = db.scalar(
        select(PaymentException).where(
            PaymentException.correlation_id == payment_row.correlation_id,
            PaymentException.kind == _ORPHANED_MONEY,
        )
    )
    if seen is not None:
        return False
    db.add(
        PaymentException(
            correlation_id=payment_row.correlation_id,
            payment_id=payment_row.id,
            kind=_ORPHANED_MONEY,
            local_state={"checkout_id": checkout.id, "checkout_state": checkout.state},
            gateway_state=entity,
        )
    )
    return True


def _gateway_error(entity: dict[str, Any]) -> dict[str, Any]:
    """The failure fields Razorpay sets on a failed payment, for the evidence packet."""
    fields = ("error_code", "error_description", "error_source", "error_step", "error_reason")
    return {key: entity[key] for key in fields if entity.get(key)}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    signature: Annotated[str | None, Header(alias="X-Razorpay-Signature")] = None,
) -> dict[str, Any]:
    raw = await request.body()
    if not verify_webhook_signature(raw, signature):
        # Rejected before parsing. Parsing and re-serialising would also break the hash.
        raise AgentError(
            ReasonCode.WEBHOOK_SIGNATURE_INVALID,
            "the webhook signature did not verify",
            status_code=401,
        )

    payload = json.loads(raw)
    event = str(payload.get("event", ""))
    entities = payload.get("payload") or {}
    handled: list[str] = []

    payment_entity = ((entities.get("payment") or {}).get("entity")) or {}
    refund_entity = ((entities.get("refund") or {}).get("entity")) or {}

    if payment_entity:
        row = db.scalar(
            select(Payment).where(Payment.razorpay_payment_id == str(payment_entity.get("id")))
        )
        if row is None and payment_entity.get("order_id"):
            row = db.scalar(
                select(Payment).where(
                    Payment.razorpay_order_id == str(payment_entity["order_id"])
                )
            )
        if row is not None:
            snapshot = dict(row.gateway_snapshot or {})
            snapshot.setdefault("webhooks", []).append({"event": event, "entity": payment_entity})
            row.gateway_snapshot = snapshot
            if event == "payment.captured":
                row.razorpay_payment_id = str(payment_entity.get("id"))
                row.status = PaymentStatus.CAPTURED
                row.captured_at = utcnow()
                db.flush()
                packet_id = finalise_captured(db, row)
                if packet_id:
                    handled.append("checkout.finalised")
            elif event == "payment.authorized":
                row.razorpay_payment_id = str(payment_entity.get("id"))
                if row.status == PaymentStatus.CREATED:
                    row.status = PaymentStatus.AUTHORIZED
            elif event == "payment.failed":
                row.status = PaymentStatus.FAILED
                packet_id = finalise_failed(db, row, error=_gateway_error(payment_entity))
                if packet_id:
                    logger.info(
                        "checkout cancelled after a failed payment",
                        extra={"context": {"evidence_packet_id": packet_id}},
                    )
            if _flag_orphaned_money(db, row, payment_entity):
                handled.append("reconciliation.exception")
                logger.warning(
                    "gateway holds money for a checkout that will not be settled",
                    extra={"context": {"correlation_id": row.correlation_id}},
                )
            handled.append(event)

    if refund_entity:
        row = db.scalar(
            select(Refund).where(Refund.razorpay_refund_id == str(refund_entity.get("id")))
        )
        if row is not None:
            row.gateway_snapshot = refund_entity
            if event == "refund.processed":
                row.status = RefundStatus.PROCESSED
            elif event == "refund.failed":
                row.status = RefundStatus.FAILED
                # A compensating refund that fails after the fact leaves the checkout claiming it
                # was compensated while the buyer never got the money back. That disagreement is
                # filed, never silently corrected.
                db.add(
                    PaymentException(
                        correlation_id=row.correlation_id,
                        payment_id=row.payment_id,
                        kind="refund_failed_after_creation",
                        local_state={
                            "refund_id": row.id,
                            "amount_minor": row.amount_minor,
                            "compensating": row.compensating,
                        },
                        gateway_state=refund_entity,
                    )
                )
            handled.append(event)

    logger.info(
        "razorpay webhook accepted",
        extra={"context": {"event": event, "handled": handled}},
    )
    return {"received": True, "event": event, "handled": handled}


@router.get("/webhooks/whatsapp")
def whatsapp_subscription(
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    """Meta's subscription handshake. The challenge is echoed only for the right verify token."""
    challenge = whatsapp.verify_subscription(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        return Response(status_code=403, content="verification failed", media_type="text/plain")
    return Response(status_code=200, content=challenge, media_type="text/plain")


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, Any]:
    raw = await request.body()
    if not whatsapp.verify_signature(raw, signature):
        raise AgentError(
            ReasonCode.WEBHOOK_SIGNATURE_INVALID,
            "the webhook signature did not verify",
            status_code=401,
        )

    payload = json.loads(raw)
    applied: list[dict[str, Any]] = []
    for answer in whatsapp.parse_inbound(payload):
        if not answer.escalation_id:
            applied.append({"answer": answer.answer, "ignored": "no_escalation_reference"})
            continue
        outcome = escalation_service.record_answer(
            db, answer.escalation_id, answer.answer, message_id=answer.message_id
        )
        applied.append(
            {
                "escalation_id": answer.escalation_id,
                "answer": answer.answer,
                "accepted": outcome.accepted,
                "status": outcome.status,
                "ignored_reason": outcome.ignored_reason,
            }
        )
    return {"received": True, "applied": applied}
