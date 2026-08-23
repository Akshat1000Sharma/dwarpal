"""Signed, reason-coded policy verdicts.

A verdict cannot be constructed without a reason code from the closed set, and every verdict is
signed by the merchant key before it is stored. Money never moves without one of these recorded
first; the payment path takes a persisted verdict id as a required argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.ap2.jose import hash_payload, sign_jws
from app.ap2.vocabulary import VERDICT_JWT_TYP
from app.correlation import get_correlation_id
from app.db.base import utcnow
from app.db.models import Verdict as VerdictRow
from app.kernel.reasons import AgentAction, Decision, ReasonCode, action_for, is_approval
from app.keys import merchant_key
from app.settings import settings


class KernelAction:
    """The money-adjacent operations the kernel gates."""

    QUOTE = "quote"
    HOLD = "hold"
    CHECKOUT = "checkout"
    CAPTURE = "capture"
    REFUND = "refund"
    COMPENSATE = "compensate"


@dataclass(frozen=True)
class Verdict:
    """A decision, the reason for it, and the evidence it was decided on."""

    decision: Decision
    reason_code: ReasonCode
    action: str
    agent_id: str
    amount_minor: int = 0
    currency: str = "INR"
    checkout_id: str | None = None
    mandate_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    decided_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, ReasonCode):
            raise TypeError("a verdict must carry a ReasonCode from the closed set")
        if self.decision is Decision.ALLOW and not is_approval(self.reason_code):
            raise ValueError(
                f"{self.reason_code.value} is not an approval code and cannot allow an action"
            )
        if self.decision is not Decision.ALLOW and is_approval(self.reason_code):
            raise ValueError(f"{self.reason_code.value} is an approval code and cannot refuse")

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def agent_action(self) -> AgentAction:
        return action_for(self.reason_code)

    def to_claims(self) -> dict[str, Any]:
        return {
            "iss": settings.MERCHANT_ID,
            "iat": int(self.decided_at.timestamp()),
            "typ": "policy-verdict",
            "correlation_id": self.correlation_id or get_correlation_id(),
            "action": self.action,
            "decision": self.decision.value,
            "reason_code": self.reason_code.value,
            "agent_action": self.agent_action.value,
            "agent_id": self.agent_id,
            "checkout_id": self.checkout_id,
            "mandate_id": self.mandate_id,
            "amount": {"amount": self.amount_minor, "currency": self.currency},
            "evidence_hash": hash_payload(self.evidence),
        }

    def sign(self) -> str:
        return sign_jws(self.to_claims(), merchant_key(), typ=VERDICT_JWT_TYP)


def allow(
    reason_code: ReasonCode,
    action: str,
    agent_id: str,
    **kwargs: Any,
) -> Verdict:
    return Verdict(
        decision=Decision.ALLOW,
        reason_code=reason_code,
        action=action,
        agent_id=agent_id,
        correlation_id=kwargs.pop("correlation_id", get_correlation_id()),
        **kwargs,
    )


def refuse(
    reason_code: ReasonCode,
    action: str,
    agent_id: str,
    *,
    decision: Decision = Decision.DENY,
    **kwargs: Any,
) -> Verdict:
    return Verdict(
        decision=decision,
        reason_code=reason_code,
        action=action,
        agent_id=agent_id,
        correlation_id=kwargs.pop("correlation_id", get_correlation_id()),
        **kwargs,
    )


def record(session: Session, verdict: Verdict) -> VerdictRow:
    """Persist and sign. The returned row id is what the payment path requires."""
    row = VerdictRow(
        correlation_id=verdict.correlation_id or get_correlation_id(),
        checkout_id=verdict.checkout_id,
        agent_id=verdict.agent_id,
        mandate_id=verdict.mandate_id,
        action=verdict.action,
        decision=verdict.decision.value,
        reason_code=verdict.reason_code.value,
        amount_minor=verdict.amount_minor,
        currency=verdict.currency,
        evidence=verdict.evidence,
        signed_jwt=verdict.sign(),
        created_at=verdict.decided_at,
    )
    session.add(row)
    session.flush()
    return row
