"""The buyer console's surface.

This is a demonstration and operations surface, not a second purchase path. Every run it starts
goes through the same quote, the same verification pipeline, the same policy kernel and the same
evidence locker an external agent reaches over HTTP. What it adds is a log you can watch.

It is guarded by the merchant surface for the same reason that surface is: the documented runbook
tunnels this port to the public internet. Somebody else's agent transacts against the agent
endpoints with a connection token, never through here.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_merchant_token
from app.buyer import runner
from app.buyer.planner import DEFAULT_BUDGET_MINOR
from app.checkout.complete import finalise_captured
from app.db.base import utcnow
from app.db.models import BuyerRun, BuyerRunStatus, Payment, PaymentStatus
from app.logging import get_logger
from app.notify import service as receipts
from app.payments import service as payments
from app.payments.gateway import (
    GatewayError,
    live_gateway_configured,
    verify_checkout_signature,
)
from app.settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/buyer", tags=["buyer"], dependencies=[Depends(require_merchant_token)])

# Razorpay's documented test card. It is published by Razorpay, is only accepted in test mode, and
# is the reason the application refuses to start against a live key.
TEST_CARD = {
    "number": "4111 1111 1111 1111",
    "expiry": "12/30",
    "cvv": "123",
    "name": "Dwarpal Test Buyer",
    "note": "Razorpay test mode only. No real money can move with these details.",
}


class RunBody(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    budget_cap_minor: int | None = Field(default=None, ge=0, le=100_000_000)
    natural_language: list[str] = Field(default_factory=list)
    connection_id: str | None = None
    human_present: bool = False


class PayBody(BaseModel):
    """Exactly what Razorpay Checkout hands to the page's handler."""

    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.get("/gateway")
def gateway_mode() -> dict[str, Any]:
    """Whether the hosted Razorpay Checkout is usable here, and the card to type into it."""
    live = live_gateway_configured()
    return {
        "mode": "razorpay" if live else "stub",
        "key_id": settings.RAZORPAY_KEY_ID if live else None,
        "test_card": TEST_CARD,
        "merchant": {"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME},
        "explanation": (
            "Razorpay test-mode Checkout is configured, so the payment is real API traffic against "
            "a test key."
            if live
            else "No usable Razorpay test key is configured, so orders are settled by the "
            "deterministic stub gateway instead. The policy decisions are identical either way."
        ),
    }


@router.get("/defaults")
def defaults() -> dict[str, Any]:
    """What the console pre-fills, so the copy lives on the server with the behaviour."""
    return {
        "budget_cap_minor": DEFAULT_BUDGET_MINOR,
        "suggested_prompts": [
            "Buy me two packets of tea and a notebook, under 2000 rupees",
            "Get a mechanical keyboard, nothing over 8000 rupees",
            "Order some fresh fruit for the office, nothing perishable",
            "Buy a bottle of wine for a gift",
        ],
        "constraints": [
            "nothing perishable",
            "nothing age restricted",
            "nothing bladed",
            "vegetarian items only",
        ],
    }


@router.post("/runs")
def start_run(body: RunBody, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    """Start an agent run and return immediately, so the console can watch it work."""
    request = runner.RunRequest(
        prompt=body.prompt,
        budget_cap_minor=body.budget_cap_minor,
        natural_language=list(body.natural_language),
        human_present=body.human_present,
        connection_id=body.connection_id,
    )
    run = runner.create_run(db, request)
    agent_id = run.agent_id
    run_id = run.id
    # The run row must be visible to the worker's own session before it starts.
    db.commit()
    runner.start(run_id, agent_id, request)
    return {"run_id": run_id, "agent_id": agent_id, "status": run.status}


@router.get("/runs")
def list_runs(db: Annotated[Session, Depends(get_db)], limit: int = 25) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(BuyerRun).order_by(desc(BuyerRun.created_at)).limit(max(1, min(limit, 100)))
        ).all()
    )
    return {"runs": [_run_document(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    run = db.get(BuyerRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    document = _run_document(run)
    document["events"] = [
        {
            "seq": e.seq,
            "level": e.level,
            "step": e.step,
            "message": e.message,
            "data": e.data,
            "duration_ms": e.duration_ms,
            "at": e.at.isoformat(),
        }
        for e in runner.events_for(db, run_id)
    ]
    document["receipts"] = [
        {
            "kind": n.kind,
            "status": n.status,
            "route": n.route,
            "error": n.error,
            "at": n.created_at.isoformat(),
        }
        for n in receipts.for_correlation(db, run.correlation_id)
    ]
    return document


@router.post("/runs/{run_id}/pay")
def pay_run(
    run_id: str, body: PayBody, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    """Settle a run whose order Razorpay Checkout has just paid.

    The browser is untrusted, so the handler's HMAC is verified before anything moves. Capture
    then goes through the ordinary payments service, which re-checks that an approving verdict was
    recorded first, so this path cannot move money the kernel did not authorise.
    """
    run = db.get(BuyerRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    if run.status != BuyerRunStatus.AWAITING_PAYMENT.value:
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} is {run.status}, so there is nothing waiting to be paid",
        )
    if not verify_checkout_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        raise HTTPException(status_code=401, detail="the Razorpay handler signature did not verify")

    # FOR UPDATE, because the signed webhook for this order may be in flight on another worker:
    # without it both readers see AUTHORIZED, pass the guard below, and capture. populate_existing
    # re-reads the row rather than handing back a copy taken before the lock was granted.
    payment = db.scalar(
        select(Payment)
        .where(Payment.razorpay_order_id == body.razorpay_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None or payment.checkout_id != run.checkout_id:
        raise HTTPException(status_code=404, detail="no payment matches that order for this run")

    if payment.status == PaymentStatus.CAPTURED:
        # The webhook beat the handler back. Both arriving is normal and must be harmless.
        return {"run_id": run_id, "status": run.status, "already_captured": True}

    payment.razorpay_payment_id = body.razorpay_payment_id
    if payment.status == PaymentStatus.CREATED:
        payment.status = PaymentStatus.AUTHORIZED
    db.flush()

    try:
        payments.capture(db, verdict_id=payment.verdict_id, payment=payment)
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=f"capture failed at Razorpay: {exc}") from exc

    packet_id = finalise_captured(db, payment)
    run.payment_id = payment.id
    run.status = BuyerRunStatus.COMPLETED.value
    run.finished_at = utcnow()
    if packet_id:
        run.evidence_packet_id = packet_id
    db.flush()
    return {
        "run_id": run_id,
        "status": run.status,
        "payment_id": payment.razorpay_payment_id,
        "evidence_packet_id": run.evidence_packet_id,
    }


def _run_document(run: BuyerRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "prompt": run.prompt,
        "planner": run.planner,
        "agent_id": run.agent_id,
        "status": run.status,
        "correlation_id": run.correlation_id,
        "checkout_id": run.checkout_id,
        "razorpay_order_id": run.razorpay_order_id,
        "reason_code": run.reason_code,
        "evidence_packet_id": run.evidence_packet_id,
        "amount": {
            "amount": run.amount_minor,
            "currency": run.currency,
            "display": f"{run.currency} {run.amount_minor / 100:,.2f}",
        },
        "plan": run.plan,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
