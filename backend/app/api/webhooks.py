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
from app.checkout.complete import finalise_captured
from app.db.base import utcnow
from app.db.models import Payment, PaymentStatus, Refund, RefundStatus
from app.errors import AgentError
from app.escalation import service as escalation_service
from app.escalation import whatsapp
from app.kernel.reasons import ReasonCode
from app.logging import get_logger
from app.payments.gateway import verify_webhook_signature

logger = get_logger(__name__)
router = APIRouter(tags=["webhooks"])


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
