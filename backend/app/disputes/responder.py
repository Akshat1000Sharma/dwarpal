"""Dispute representment and the contest-or-refund recommendation.

Given a claim that a transaction was unauthorised, this assembles the evidence packet into a
representment: what authority was presented, what constraints the human set, how the cart
satisfied them, what the buyer acknowledged, and when each step occurred.

It also scores its own evidence. A responder that recommends contesting everything is worthless,
so the scoring is explicit, the thresholds are named, and weak evidence produces a refund
recommendation with the reasons stated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Dispute, DisputeOutcome
from app.evidence import locker

CONTEST_THRESHOLD = 70
BORDERLINE_THRESHOLD = 45


class Recommendation(StrEnum):
    CONTEST = "contest"
    REFUND = "refund"


@dataclass
class EvidenceFactor:
    """One named check, its weight, and whether the packet satisfies it."""

    key: str
    description: str
    weight: int
    present: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "weight": self.weight,
            "present": self.present,
            "awarded": self.weight if self.present else 0,
            "detail": self.detail,
        }


@dataclass
class Representment:
    correlation_id: str
    recommendation: Recommendation
    strength_score: int
    factors: list[EvidenceFactor] = field(default_factory=list)
    narrative: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    packet_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "recommendation": self.recommendation.value,
            "strength_score": self.strength_score,
            "contest_threshold": CONTEST_THRESHOLD,
            "factors": [f.as_dict() for f in self.factors],
            "narrative": self.narrative,
            "weaknesses": self.weaknesses,
            "timeline": self.timeline,
            "evidence_packets": self.packet_ids,
        }


def _factors(body: dict[str, Any]) -> list[EvidenceFactor]:
    """The named checks. Weights are relative and scored against what was achievable."""
    chain = body.get("credential_chain") or {}
    verification = body.get("verification") or {}
    checkout = body.get("checkout") or {}
    verdicts = body.get("verdicts") or []
    payments = body.get("payments") or []
    escalations = body.get("escalations") or []
    semantic = body.get("semantic_checks") or []

    approving = [v for v in verdicts if v.get("decision") == "allow"]
    steps_passed = verification.get("steps_passed") or []
    constraints = verification.get("constraints") or []
    satisfied = [c for c in constraints if c.get("outcome") == "satisfied"]
    unresolved = [c for c in constraints if c.get("outcome") == "unresolved"]

    return [
        EvidenceFactor(
            "human_authority",
            "An open Checkout Mandate signed by the human was presented",
            20,
            bool(chain.get("open_checkout_mandate")),
            "the human's own signed authorisation is the foundation of any defence",
        ),
        EvidenceFactor(
            "agent_key_binding",
            "The presenting agent proved possession of the key the mandate was issued to",
            15,
            "subject_binding" in steps_passed,
            "without this the credential could have been presented by anyone who obtained it",
        ),
        EvidenceFactor(
            "issuer_trusted",
            "The issuing authority resolved in the trust registry",
            10,
            "issuer_trust" in steps_passed,
            f"issuer {verification.get('issuer_id', 'unknown')}",
        ),
        EvidenceFactor(
            "checkout_binding",
            "The closed mandate bound to a Checkout this merchant signed",
            15,
            "checkout_binding" in steps_passed,
            "proves the cart and total were not altered after the merchant committed to them",
        ),
        EvidenceFactor(
            "constraints_satisfied",
            "Every constraint the human set was evaluated and satisfied deterministically",
            15,
            bool(satisfied) and not unresolved,
            f"{len(satisfied)} satisfied, {len(unresolved)} unresolved",
        ),
        EvidenceFactor(
            "policy_acknowledged",
            "The buyer acknowledged the policy hash live at quote time",
            10,
            bool(checkout.get("policy_hash")),
            "shows the buyer was told the terms before committing",
        ),
        EvidenceFactor(
            "catalog_snapshot",
            "A price and availability snapshot from quote time is on file",
            5,
            bool(checkout.get("catalog_snapshot")),
            "reconstructs exactly what the buyer was shown",
        ),
        EvidenceFactor(
            "signed_verdict",
            "A signed, reason-coded policy verdict preceded the payment",
            5,
            bool(approving) and all(v.get("signed_jwt") for v in approving),
            "shows the money action was gated, not incidental",
        ),
        EvidenceFactor(
            "payment_record",
            "The payment record and its gateway confirmation are on file",
            5,
            bool(payments) and any(p.get("razorpay_payment_id") for p in payments),
            "ties the authority to the money that actually moved",
        ),
        EvidenceFactor(
            "human_confirmed",
            "The human was asked directly and approved",
            0 if not escalations else 10,
            any(e.get("status") == "approved" for e in escalations),
            "a direct approval is the strongest possible answer to an unauthorised claim"
            if escalations
            else "no escalation was needed",
        ),
        EvidenceFactor(
            "no_model_in_the_decision",
            "No model influenced the decision to move money",
            5,
            not any(s.get("outcome") == "deny" for s in semantic) or bool(approving),
            "the deterministic kernel owned the decision",
        ),
    ]


def _weaknesses(body: dict[str, Any], factors: list[EvidenceFactor]) -> list[str]:
    problems = [f"{f.description} is missing" for f in factors if not f.present and f.weight > 0]
    checkout = body.get("checkout") or {}
    verification = body.get("verification") or {}

    if not verification:
        problems.append("no verification record exists, so authority cannot be shown at all")
    if body.get("outcome") == "compensated":
        problems.append(
            "the mandate was revoked after capture and the merchant already refunded, so there is "
            "nothing to contest"
        )
    unresolved = [
        c for c in (verification.get("constraints") or []) if c.get("outcome") == "unresolved"
    ]
    if unresolved:
        problems.append(
            f"{len(unresolved)} constraint(s) could not be decided mechanically, which a "
            "cardholder can characterise as the merchant guessing"
        )
    if not checkout.get("catalog_snapshot"):
        problems.append("no catalog snapshot, so what the buyer saw cannot be reconstructed")
    for escalation in body.get("escalations") or []:
        if escalation.get("status") in ("timed_out", "denied", "voided"):
            problems.append(
                f"the human was asked and the escalation ended as {escalation.get('status')}"
            )
    return problems


def _timeline(body: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    checkout = body.get("checkout") or {}
    if checkout.get("quoted_at"):
        events.append({"at": checkout["quoted_at"], "event": "cart quoted and prices frozen"})
    for verdict in body.get("verdicts") or []:
        events.append(
            {
                "at": verdict.get("created_at"),
                "event": f"policy verdict {verdict.get('decision')} ({verdict.get('reason_code')})",
            }
        )
    for escalation in body.get("escalations") or []:
        events.append({"at": escalation.get("created_at"), "event": "human asked to confirm"})
        if escalation.get("answered_at"):
            events.append(
                {
                    "at": escalation["answered_at"],
                    "event": f"human escalation resolved as {escalation.get('status')}",
                }
            )
    for payment in body.get("payments") or []:
        if payment.get("captured_at"):
            events.append({"at": payment["captured_at"], "event": "payment captured"})
    for refund in body.get("refunds") or []:
        events.append(
            {"at": refund.get("created_at"), "event": f"refund issued ({refund.get('reason')})"}
        )
    for step in body.get("timings") or []:
        if step.get("started_at"):
            events.append({"at": step["started_at"], "event": f"step: {step.get('step')}"})
    return sorted((e for e in events if e.get("at")), key=lambda e: str(e["at"]))


def _narrative(body: dict[str, Any], factors: list[EvidenceFactor]) -> list[str]:
    verification = body.get("verification") or {}
    checkout = body.get("checkout") or {}
    lines: list[str] = []

    lines.append(
        "Authority presented: an open Checkout Mandate signed by the human principal and issued to "
        f"agent {verification.get('agent_id', 'unknown')}, from issuing authority "
        f"{verification.get('issuer_id', 'unknown')} at tier "
        f"{(verification.get('tier') or {}).get('name', 'unknown')}."
    )
    constraints = verification.get("constraints") or []
    satisfied = [c["constraint_type"] for c in constraints if c.get("outcome") == "satisfied"]
    if satisfied:
        lines.append(
            "Constraints the human set, and which the cart satisfied: " + ", ".join(satisfied) + "."
        )
    lines.append(
        "The agent proved possession of the key the mandate was issued to, and the closed mandate "
        "bound to a Checkout this merchant had itself signed, covering the cart contents, the "
        "total and the policy hash."
        if all(f.present for f in factors if f.key in ("agent_key_binding", "checkout_binding"))
        else "The credential chain did not fully bind to a merchant-signed Checkout."
    )
    if checkout.get("policy_hash"):
        lines.append(
            f"The buyer acknowledged policy hash {checkout['policy_hash']}, which was the version "
            "live when the cart was quoted."
        )
    for escalation in body.get("escalations") or []:
        lines.append(
            "The merchant additionally contacted the human principal because a constraint could "
            f"not be decided mechanically. That escalation ended as {escalation.get('status')}."
        )
    if checkout.get("total_minor") is not None:
        lines.append(
            f"Amount charged: {checkout.get('currency', 'INR')} "
            f"{checkout['total_minor'] / 100:.2f}."
        )
    return lines


def _score(factors: list[EvidenceFactor]) -> int:
    """Percentage of the evidence that was achievable for this transaction and was actually there.

    Normalised rather than summed, because the achievable total varies: the human-confirmation
    factor only carries weight when an escalation was raised. A raw sum would put the score above
    the hundred-point scale the threshold and the representment are both stated on.
    """
    achievable = sum(f.weight for f in factors)
    if achievable <= 0:
        return 0
    return round(100 * sum(f.weight for f in factors if f.present) / achievable)


def build_representment(body: dict[str, Any], *, correlation_id: str) -> Representment:
    factors = _factors(body)
    score = _score(factors)
    weaknesses = _weaknesses(body, factors)

    # Compensated transactions are never contested: the merchant has already given the money back.
    if body.get("outcome") == "compensated":
        score = 0
    recommendation = (
        Recommendation.CONTEST if score >= CONTEST_THRESHOLD else Recommendation.REFUND
    )
    if recommendation is Recommendation.REFUND and score >= BORDERLINE_THRESHOLD:
        weaknesses.insert(
            0,
            f"the evidence scores {score}, below the {CONTEST_THRESHOLD} threshold for contesting; "
            "the cost of losing a contested dispute exceeds the value of the sale",
        )

    return Representment(
        correlation_id=correlation_id,
        recommendation=recommendation,
        strength_score=score,
        factors=factors,
        narrative=_narrative(body, factors),
        weaknesses=weaknesses,
        timeline=_timeline(body),
    )


# Claims proved once at verification time and not restated by later packets in the same chain.
_INHERITED_CLAIMS = ("credential_chain", "verification", "checkout", "semantic")


def _consolidated(bodies: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the whole chain for one transaction, not just its last link.

    The settled outcome is the last packet, but authority is proved in the packet written when the
    credentials were verified, which for any webhook-driven settlement is an earlier one. Defending
    on the last packet alone discards evidence that was recorded correctly. Packets are append-only,
    so consolidating at read time is also what makes already-written chains defensible.
    """
    body = dict(bodies[-1])
    for claim in _INHERITED_CLAIMS:
        if body.get(claim):
            continue
        for earlier in reversed(bodies[:-1]):
            if earlier.get(claim):
                body[claim] = earlier[claim]
                break
    return body


def respond(session: Session, *, correlation_id: str, claim: str) -> Dispute:
    """Assemble a representment from stored evidence and record the recommendation."""
    packets = locker.for_correlation(session, correlation_id)
    if not packets:
        representment = Representment(
            correlation_id=correlation_id,
            recommendation=Recommendation.REFUND,
            strength_score=0,
            weaknesses=["no evidence packet exists for this transaction"],
        )
    else:
        representment = build_representment(
            _consolidated([p.body for p in packets]), correlation_id=correlation_id
        )
        representment.packet_ids = [p.packet_id for p in packets]

    row = Dispute(
        correlation_id=correlation_id,
        claim=claim,
        claimed_at=utcnow(),
        recommendation=representment.recommendation.value,
        strength_score=representment.strength_score,
        representment=representment.as_dict(),
        outcome=DisputeOutcome.OPEN,
    )
    session.add(row)
    session.flush()
    return row


def score_without_evidence(claim: str) -> Representment:
    """The baseline: what a merchant with no evidence packet can say. It is nothing."""
    return Representment(
        correlation_id="",
        recommendation=Recommendation.REFUND,
        strength_score=0,
        weaknesses=[
            "no credential chain was retained, so the buyer's authority cannot be shown",
            "no merchant-signed Checkout was retained, so the agreed cart and total cannot be "
            "shown",
            "no policy acknowledgment was retained",
            "no signed policy verdict preceded the charge",
        ],
        narrative=[f"Claim: {claim}", "The merchant holds no evidence beyond the payment record."],
    )
