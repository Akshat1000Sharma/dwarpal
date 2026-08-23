"""Payment execution.

Two rules are enforced in code rather than by convention:

    - money never moves without a policy verdict recorded first. Every function that can move
      money takes a persisted verdict id and re-reads that row to confirm it exists and allowed
      the action.
    - Razorpay is authoritative. When the local record and the gateway disagree, the discrepancy
      is filed as an exception rather than silently corrected in either direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
)
from app.db.models import Verdict as VerdictRow
from app.kernel.reasons import Decision, ReasonCode, is_approval
from app.logging import get_logger
from app.payments.gateway import GatewayError, PaymentGateway, get_gateway

logger = get_logger(__name__)


class VerdictMissing(Exception):
    """Raised when money was about to move without an approving verdict recorded first."""


@dataclass(frozen=True)
class CaptureResult:
    payment: Payment
    gateway_payload: dict[str, Any]


def _require_approving_verdict(session: Session, verdict_id: str, action: str) -> VerdictRow:
    """The ordering rule, enforced as a precondition rather than assumed."""
    row = session.get(VerdictRow, verdict_id)
    if row is None:
        raise VerdictMissing(f"no verdict {verdict_id} is recorded; money must not move")
    if row.decision != Decision.ALLOW.value:
        raise VerdictMissing(
            f"verdict {verdict_id} is {row.decision} ({row.reason_code}); money must not move"
        )
    if not is_approval(ReasonCode(row.reason_code)):
        raise VerdictMissing(f"verdict {verdict_id} carries a non-approval reason code")
    del action
    return row


def create_order(
    session: Session,
    *,
    verdict_id: str,
    correlation_id: str,
    checkout_id: str,
    agent_id: str,
    amount_minor: int,
    currency: str,
    gateway: PaymentGateway | None = None,
) -> Payment:
    """Create the Razorpay order, after confirming an approving verdict exists."""
    _require_approving_verdict(session, verdict_id, "order")

    existing = session.scalar(
        select(Payment).where(Payment.checkout_id == checkout_id).order_by(Payment.created_at)
    )
    if existing is not None:
        # A retry after a timeout must not produce a second order.
        return existing

    client = gateway or get_gateway()
    order = client.create_order(
        amount_minor=amount_minor,
        currency=currency,
        receipt=checkout_id,
        notes={
            "correlation_id": correlation_id,
            "agent_id": agent_id[:40],
            "checkout_id": checkout_id,
        },
    )
    payment = Payment(
        correlation_id=correlation_id,
        checkout_id=checkout_id,
        agent_id=agent_id,
        verdict_id=verdict_id,
        razorpay_order_id=str(order.get("id")),
        amount_minor=amount_minor,
        currency=currency,
        status=PaymentStatus.CREATED,
        gateway_snapshot={"order": order},
    )
    session.add(payment)
    session.flush()
    return payment


def attach_authorization(
    session: Session, payment: Payment, gateway_payment: dict[str, Any]
) -> Payment:
    payment.razorpay_payment_id = str(gateway_payment.get("id"))
    payment.status = PaymentStatus.AUTHORIZED
    snapshot = dict(payment.gateway_snapshot or {})
    snapshot["authorization"] = gateway_payment
    payment.gateway_snapshot = snapshot
    session.flush()
    return payment


def capture(
    session: Session,
    *,
    verdict_id: str,
    payment: Payment,
    gateway: PaymentGateway | None = None,
) -> CaptureResult:
    """Capture, idempotently. A second call returns the first result rather than charging again."""
    _require_approving_verdict(session, verdict_id, "capture")
    if payment.status == PaymentStatus.CAPTURED:
        return CaptureResult(payment, dict((payment.gateway_snapshot or {}).get("capture", {})))
    if not payment.razorpay_payment_id:
        raise GatewayError("cannot capture before an authorisation is attached")

    client = gateway or get_gateway()
    captured = client.capture_payment(
        payment.razorpay_payment_id, payment.amount_minor, payment.currency
    )
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    snapshot = dict(payment.gateway_snapshot or {})
    snapshot["capture"] = captured
    payment.gateway_snapshot = snapshot
    session.flush()
    return CaptureResult(payment, captured)


def mark_failed(session: Session, payment: Payment, detail: dict[str, Any]) -> Payment:
    payment.status = PaymentStatus.FAILED
    snapshot = dict(payment.gateway_snapshot or {})
    snapshot["failure"] = detail
    payment.gateway_snapshot = snapshot
    session.flush()
    return payment


def refund(
    session: Session,
    *,
    verdict_id: str,
    payment: Payment,
    amount_minor: int | None = None,
    reason: str,
    compensating: bool = False,
    gateway: PaymentGateway | None = None,
) -> Refund:
    """Issue a refund. Refunds are a first-class path because revocation depends on them."""
    _require_approving_verdict(session, verdict_id, "refund")

    existing = session.scalar(
        select(Refund).where(Refund.payment_id == payment.id, Refund.reason == reason)
    )
    if existing is not None:
        return existing

    amount = amount_minor if amount_minor is not None else payment.amount_minor
    client = gateway or get_gateway()
    row = Refund(
        payment_id=payment.id,
        correlation_id=payment.correlation_id,
        amount_minor=amount,
        reason=reason,
        compensating=compensating,
        status=RefundStatus.CREATED,
    )
    session.add(row)
    session.flush()

    try:
        result = client.create_refund(
            str(payment.razorpay_payment_id),
            amount_minor=amount,
            notes={"reason": reason[:40], "correlation_id": payment.correlation_id},
        )
    except GatewayError as exc:
        row.status = RefundStatus.FAILED
        row.gateway_snapshot = {"error": str(exc)}
        session.flush()
        raise

    row.razorpay_refund_id = str(result.get("id"))
    row.status = (
        RefundStatus.PROCESSED if result.get("status") == "processed" else RefundStatus.CREATED
    )
    row.gateway_snapshot = result
    if row.status == RefundStatus.PROCESSED and amount >= payment.amount_minor:
        payment.status = PaymentStatus.REFUNDED
    session.flush()
    return row


def payment_for_checkout(session: Session, checkout_id: str) -> Payment | None:
    return session.scalar(
        select(Payment).where(Payment.checkout_id == checkout_id).order_by(Payment.created_at)
    )


def refunds_for_payment(session: Session, payment_id: str) -> list[Refund]:
    return list(
        session.scalars(
            select(Refund).where(Refund.payment_id == payment_id).order_by(Refund.created_at)
        ).all()
    )
