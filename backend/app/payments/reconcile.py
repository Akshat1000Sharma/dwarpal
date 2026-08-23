"""Reconciliation of the local payment record against Razorpay.

Razorpay is authoritative. When the two disagree the discrepancy is filed as an exception rather
than silently corrected in either direction, because a silent correction erases the evidence that
they ever disagreed, which is exactly what an operator needs to see.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Payment, PaymentException, PaymentStatus
from app.logging import get_logger
from app.payments.gateway import GatewayError, PaymentGateway, get_gateway

logger = get_logger(__name__)

# The gateway states that do not contradict a given local state. Anything else is a disagreement.
CONSISTENT_WITH: dict[str, set[str]] = {
    PaymentStatus.CAPTURED: {"captured", "refunded"},
    PaymentStatus.AUTHORIZED: {"authorized", "captured", "refunded"},
    PaymentStatus.CREATED: {"created", "authorized"},
    PaymentStatus.FAILED: {"failed"},
    PaymentStatus.REFUNDED: {"refunded", "captured"},
}


def record_exception(
    session: Session, payment: Payment, kind: str, detail: dict[str, Any]
) -> PaymentException:
    row = PaymentException(
        correlation_id=payment.correlation_id,
        payment_id=payment.id,
        kind=kind,
        local_state={"status": payment.status, "amount_minor": payment.amount_minor},
        gateway_state=detail,
    )
    session.add(row)
    session.flush()
    logger.warning(
        "payment exception recorded",
        extra={"context": {"kind": kind, "payment_id": payment.id}},
    )
    return row


def reconcile(
    session: Session, payment: Payment, *, gateway: PaymentGateway | None = None
) -> PaymentException | None:
    """Compare the local record against Razorpay. Razorpay wins; disagreement is recorded."""
    client = gateway or get_gateway()
    if not payment.razorpay_payment_id:
        return None
    try:
        remote = client.fetch_payment(payment.razorpay_payment_id)
    except GatewayError as exc:
        return record_exception(session, payment, "gateway_unreachable", {"error": str(exc)})

    local_state = {
        "status": payment.status,
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
    }
    remote_status = str(remote.get("status"))
    remote_amount = int(remote.get("amount", 0))
    mismatches: dict[str, Any] = {}

    if remote_status not in CONSISTENT_WITH.get(PaymentStatus(payment.status), set()):
        mismatches["status"] = {"local": payment.status, "gateway": remote_status}
    if remote_amount and remote_amount != payment.amount_minor:
        mismatches["amount"] = {"local": payment.amount_minor, "gateway": remote_amount}

    payment.reconciled_at = utcnow()
    snapshot = dict(payment.gateway_snapshot or {})
    snapshot["reconciliation"] = remote
    payment.gateway_snapshot = snapshot
    session.flush()

    if not mismatches:
        return None
    return record_exception(
        session,
        payment,
        "reconciliation_discrepancy",
        {"mismatches": mismatches, "gateway": remote, "local": local_state},
    )
