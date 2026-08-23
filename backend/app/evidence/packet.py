"""Assembly of the evidence packet body.

The packet has to be sufficient for a third party to re-check the transaction from stored data
alone: the full credential chain presented, a snapshot of the exact catalog state and prices that
were live when the cart was quoted, the acknowledged policy hash, every policy verdict with its
reason codes, the escalation record if there was one, timing for each step, and the Razorpay
payment and refund records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ap2.vocabulary import AP2_PROTOCOL_VERSION, AP2_SCHEMA_REVISION
from app.db.base import utcnow
from app.db.models import (
    CheckoutSession,
    Escalation,
    Payment,
    Refund,
)
from app.db.models import Verdict as VerdictRow
from app.escalation import service as escalation_service
from app.keys import merchant_jwks
from app.settings import settings


@dataclass
class StepTiming:
    name: str
    started_at: datetime
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        duration = None
        if self.finished_at is not None:
            duration = round((self.finished_at - self.started_at).total_seconds() * 1000, 3)
        return {
            "step": self.name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": duration,
        }


@dataclass
class TimingRecorder:
    """Per-step timing, carried through the checkout flow into the packet."""

    steps: list[StepTiming] = field(default_factory=list)

    def start(self, name: str) -> StepTiming:
        step = StepTiming(name=name, started_at=utcnow())
        self.steps.append(step)
        return step

    def finish(self, step: StepTiming) -> None:
        step.finished_at = utcnow()

    def mark(self, name: str) -> None:
        step = self.start(name)
        self.finish(step)

    def as_list(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.steps]


def build(
    session: Session,
    *,
    correlation_id: str,
    outcome: str,
    agent_id: str,
    credentials: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    checkout_row: CheckoutSession | None,
    semantic: list[dict[str, Any]] | None = None,
    timings: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect everything recorded under this correlation id into one immutable body."""
    verdict_rows = list(
        session.scalars(
            select(VerdictRow)
            .where(VerdictRow.correlation_id == correlation_id)
            .order_by(VerdictRow.created_at)
        ).all()
    )
    payments = list(
        session.scalars(
            select(Payment)
            .where(Payment.correlation_id == correlation_id)
            .order_by(Payment.created_at)
        ).all()
    )
    refunds = list(
        session.scalars(
            select(Refund)
            .where(Refund.correlation_id == correlation_id)
            .order_by(Refund.created_at)
        ).all()
    )
    escalations = list(
        session.scalars(
            select(Escalation)
            .where(Escalation.correlation_id == correlation_id)
            .order_by(Escalation.created_at)
        ).all()
    )

    return {
        "schema": "dwarpal.evidence.1",
        "correlation_id": correlation_id,
        "outcome": outcome,
        "agent_id": agent_id,
        "recorded_at": utcnow().isoformat(),
        "merchant": {
            "id": settings.MERCHANT_ID,
            "name": settings.MERCHANT_NAME,
            "jwks": merchant_jwks(),
        },
        "protocol": {"ap2_version": AP2_PROTOCOL_VERSION, "schema_revision": AP2_SCHEMA_REVISION},
        # The full chain exactly as presented, so a third party can re-verify every signature.
        "credential_chain": credentials or {},
        "verification": verification or {},
        "checkout": _checkout_evidence(checkout_row),
        "verdicts": [
            {
                "id": v.id,
                "action": v.action,
                "decision": v.decision,
                "reason_code": v.reason_code,
                "amount": {"amount": v.amount_minor, "currency": v.currency},
                "evidence": v.evidence,
                "signed_jwt": v.signed_jwt,
                "created_at": v.created_at.isoformat(),
            }
            for v in verdict_rows
        ],
        "semantic_checks": semantic or [],
        "escalations": [escalation_service.as_evidence(session, e) for e in escalations],
        "payments": [
            {
                "id": p.id,
                "verdict_id": p.verdict_id,
                "razorpay_order_id": p.razorpay_order_id,
                "razorpay_payment_id": p.razorpay_payment_id,
                "amount": {"amount": p.amount_minor, "currency": p.currency},
                "status": p.status,
                "captured_at": p.captured_at.isoformat() if p.captured_at else None,
                "reconciled_at": p.reconciled_at.isoformat() if p.reconciled_at else None,
                "gateway": p.gateway_snapshot,
            }
            for p in payments
        ],
        "refunds": [
            {
                "id": r.id,
                "razorpay_refund_id": r.razorpay_refund_id,
                "amount": {"amount": r.amount_minor, "currency": "INR"},
                "reason": r.reason,
                "status": r.status,
                "compensating": r.compensating,
                "gateway": r.gateway_snapshot,
                "created_at": r.created_at.isoformat(),
            }
            for r in refunds
        ],
        "timings": timings or [],
        "extra": extra or {},
    }


def _checkout_evidence(row: CheckoutSession | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "checkout_id": row.id,
        "state": row.state,
        "currency": row.currency,
        "total_minor": row.total_minor,
        "policy_hash": row.policy_hash,
        "checkout": row.checkout,
        "checkout_jwt": row.checkout_jwt,
        "checkout_hash": row.checkout_hash,
        "cart_fingerprint": row.cart_fingerprint,
        # Prices and availability frozen at quote time. A reference to a mutable product row would
        # not be a snapshot, and could not reconstruct what the buyer was shown.
        "catalog_snapshot": row.catalog_snapshot,
        "quoted_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "verified_agent": row.verified,
    }
