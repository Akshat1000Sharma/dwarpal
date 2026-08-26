"""Synthetic dispute batch, and the measured defence rate.

The money number. A batch of transactions is driven through the real gate, each is then disputed,
and the representment is scored twice: once against the evidence packet Dwarpal filed, and once
against a baseline merchant that kept only the payment record. The difference is computed, not
estimated.

The batch deliberately mixes strong and weak cases. A responder that recommends contesting
everything is worthless, so the report has to show it declining to fight the weak ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.checkout import quote
from app.checkout.complete import complete
from app.db.base import utcnow
from app.disputes import responder
from app.escalation.whatsapp import RecordingTransport
from app.evidence import locker
from app.harness import factory
from app.harness.runner import reset_between_cases
from app.kernel import revocation
from app.payments.gateway import StubGateway
from app.semantic.client import KeywordSemanticClient
from app.settings import settings


@dataclass(frozen=True)
class DisputeCase:
    id: str
    cart: list[tuple[str, str, int]]
    claim: str
    variant: str = "clean"
    issuer_id: str = factory.DEFAULT_ISSUER
    natural_language: list[str] | None = None
    buyer_region: str | None = None


# The catalog, with the tier and region a legitimate purchase of each item needs. Wine and the
# chef knife are age restricted, so only a regulated authority may buy them, and the wine is not
# sold into four states.
_CATALOG: list[tuple[str, str, str, str | None]] = [
    ("DWP-TEA-001", "Nilgiri Black Tea 250g", factory.DEFAULT_ISSUER, None),
    ("DWP-COF-002", "Single Origin Coffee Beans 500g", factory.DEFAULT_ISSUER, None),
    ("DWP-MLK-003", "Fresh Paneer 400g", factory.DEFAULT_ISSUER, None),
    ("DWP-MNG-004", "Alphonso Mangoes 1kg", factory.DEFAULT_ISSUER, None),
    ("DWP-WIN-005", "Sula Cabernet Shiraz 750ml", factory.REGULATED_ISSUER, "KA"),
    ("DWP-KNF-006", "Chef Knife 8 inch", factory.REGULATED_ISSUER, None),
    ("DWP-HDP-007", "Wireless Headphones", factory.DEFAULT_ISSUER, None),
    ("DWP-KBD-008", "Mechanical Keyboard 75 percent", factory.DEFAULT_ISSUER, None),
    ("DWP-LMP-009", "Desk Lamp with Dimmer", factory.DEFAULT_ISSUER, None),
    ("DWP-LMP-010", "Desk Lamp Compact", factory.DEFAULT_ISSUER, None),
    ("DWP-NTB-011", "Hardcover Notebook A5", factory.DEFAULT_ISSUER, None),
    ("DWP-PEN-012", "Fountain Pen Medium Nib", factory.DEFAULT_ISSUER, None),
]

# The reason codes a card network actually sees. Each is scored against the same evidence, because
# a representment that only answers one kind of claim is not a dispute responder.
_CLAIMS: list[str] = [
    "the cardholder states they never authorised this purchase",
    "the cardholder does not recognise this merchant",
    "the cardholder states the amount is wrong",
    "the cardholder states the goods were never ordered",
    "the cardholder states this was a duplicate charge",
    "the cardholder states the agent exceeded its authority",
    "the cardholder states the goods never arrived",
    "the cardholder states the item was not as described",
    "the cardholder states the subscription was cancelled before this charge",
    "the cardholder states the transaction was processed after they revoked consent",
    "the cardholder states no agent of theirs was ever authorised to buy here",
    "the cardholder states the merchant charged a different amount than quoted",
    "the cardholder states they were not told the terms of sale",
    "the cardholder states the purchase was made without their knowledge",
    "the cardholder states the credential presented was not issued to their agent",
]


def _build_batch() -> list[DisputeCase]:
    """Every item, against every claim, plus the two variants where the evidence is weak.

    Generated rather than listed so the batch grows with the catalog, and so the two weak variants
    stay a fixed, visible fraction of it. A responder that recommends contesting everything is
    worthless, so the batch has to contain cases it should decline to fight.
    """
    cases: list[DisputeCase] = []
    for sku, title, issuer, region in _CATALOG:
        for index, claim in enumerate(_CLAIMS):
            cases.append(
                DisputeCase(
                    id=f"clean-{sku.lower()}-{index:02d}",
                    cart=[(sku, title, 1)],
                    claim=claim,
                    issuer_id=issuer,
                    buyer_region=region,
                )
            )

    for index, (sku, title, issuer, region) in enumerate(_CATALOG):
        cases.append(
            DisputeCase(
                id=f"revoked-after-capture-{sku.lower()}",
                cart=[(sku, title, 1)],
                claim=_CLAIMS[index % len(_CLAIMS)],
                variant="revoke_after_capture",
                issuer_id=issuer,
                buyer_region=region,
            )
        )
        cases.append(
            DisputeCase(
                id=f"no-evidence-retained-{sku.lower()}",
                cart=[(sku, title, 1)],
                claim=_CLAIMS[(index + 1) % len(_CLAIMS)],
                variant="evidence_missing",
                issuer_id=issuer,
                buyer_region=region,
            )
        )
    return cases


BATCH: list[DisputeCase] = _build_batch()


def _run_transaction(session: Session, case: DisputeCase, gateway: StubGateway) -> str:
    correlation = f"dispute_{case.id}"
    principals = factory.Principals.create(
        issuer_id=case.issuer_id, agent_id=f"agent:{case.id}", register=True
    )
    spec = factory.spec_for_cart(case.cart, natural_language=case.natural_language or [])
    quoted = quote.create_quote(
        session,
        agent_id=principals.agent_id,
        correlation_id=correlation,
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in case.cart],
    )
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce=f"nonce-{case.id}",
    )

    if case.variant == "revoke_after_capture":
        # Capture first, then revoke, then let the orchestrator find it and compensate.
        from app.checkout.complete import _upsert_open_mandate
        from app.verification.pipeline import verify

        probe = verify(session, presentation.credentials, record_nonce=False)
        if probe.ok and probe.authority is not None:
            mandate = _upsert_open_mandate(session, probe.authority)
            session.flush()
            outcome = complete(
                session,
                presentation.credentials,
                correlation_id=correlation,
                gateway=gateway,
                semantic_client=KeywordSemanticClient(),
                whatsapp=RecordingTransport(),
                audience=settings.PUBLIC_BASE_URL,
                buyer_region=case.buyer_region,
            )
            del outcome
            revocation.revoke(session, mandate.id, "principal revoked after capture")
            session.flush()
            # A second attempt is not made; the compensation is driven directly so the packet
            # records the same shape the live path produces.
            _compensate(session, correlation, gateway)
        return correlation

    complete(
        session,
        presentation.credentials,
        correlation_id=correlation,
        gateway=gateway,
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
        buyer_region=case.buyer_region,
    )
    return correlation


def _compensate(session: Session, correlation: str, gateway: StubGateway) -> None:
    """Issue the compensating refund for a revocation that landed after capture."""
    from app.kernel.reasons import ReasonCode
    from app.kernel.verdict import KernelAction, allow
    from app.kernel.verdict import record as record_verdict
    from app.payments import service as payments

    payment = payments.payment_for_checkout(session, _checkout_for(session, correlation))
    if payment is None:
        return
    verdict = record_verdict(
        session,
        allow(
            ReasonCode.APPROVED,
            KernelAction.REFUND,
            payment.agent_id,
            correlation_id=correlation,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            checkout_id=payment.checkout_id,
            evidence={"reason": "mandate revoked after capture"},
        ),
    )
    payments.refund(
        session,
        verdict_id=verdict.id,
        payment=payment,
        reason="revocation_after_capture",
        compensating=True,
        gateway=gateway,
    )
    body = {
        "schema": "dwarpal.evidence.1",
        "correlation_id": correlation,
        "outcome": "compensated",
        "agent_id": payment.agent_id,
        "recorded_at": utcnow().isoformat(),
        "credential_chain": {},
        "verification": {},
        "checkout": {},
        "verdicts": [],
        "payments": [],
        "refunds": [],
        "escalations": [],
        "semantic_checks": [],
        "timings": [],
        "extra": {"compensation": True},
    }
    locker.append(session, correlation_id=correlation, body=body)
    session.flush()


def _checkout_for(session: Session, correlation: str) -> str:
    from sqlalchemy import select

    from app.db.models import CheckoutSession

    row = session.scalar(
        select(CheckoutSession).where(CheckoutSession.correlation_id == correlation)
    )
    return row.id if row else ""


def run_batch(session: Session) -> dict[str, Any]:
    """Drive the batch, dispute every transaction, and compute both defence rates."""
    from app.catalog.policy_terms import ensure_active_terms
    from app.db.bootstrap import seed_catalog

    seed_catalog(session)
    ensure_active_terms(session)
    session.flush()

    gateway = StubGateway()
    entries: list[dict[str, Any]] = []
    with_evidence_scores: list[int] = []
    baseline_scores: list[int] = []
    refund_recommended: list[dict[str, Any]] = []

    for case in BATCH:
        reset_between_cases(session)
        correlation = _run_transaction(session, case, gateway)
        packets = locker.for_correlation(session, correlation)
        outcome = packets[-1].body.get("outcome") if packets else "unknown"

        if case.variant == "evidence_missing":
            # A merchant that retained nothing. This is the baseline made concrete.
            representment = responder.score_without_evidence(case.claim)
            representment.correlation_id = correlation
        else:
            row = responder.respond(session, correlation_id=correlation, claim=case.claim)
            representment = responder.Representment(
                correlation_id=correlation,
                recommendation=responder.Recommendation(row.recommendation or "refund"),
                strength_score=int(row.strength_score or 0),
                weaknesses=list((row.representment or {}).get("weaknesses", [])),
            )

        baseline = responder.score_without_evidence(case.claim)
        with_evidence_scores.append(representment.strength_score)
        baseline_scores.append(baseline.strength_score)

        entry = {
            "case_id": case.id,
            "correlation_id": correlation,
            "claim": case.claim,
            "transaction_outcome": outcome,
            "strength_score": representment.strength_score,
            "recommendation": representment.recommendation.value,
            "baseline_score": baseline.strength_score,
            "baseline_recommendation": baseline.recommendation.value,
            "weaknesses": representment.weaknesses,
        }
        entries.append(entry)
        if representment.recommendation is responder.Recommendation.REFUND:
            refund_recommended.append(entry)

    total = len(entries)
    defensible = len([e for e in entries if e["recommendation"] == "contest"])
    baseline_defensible = len([e for e in entries if e["baseline_recommendation"] == "contest"])

    return {
        "generated_at": utcnow().isoformat(),
        "total": total,
        "with_evidence": {
            "defensible": defensible,
            "defence_rate": round(defensible / total, 4) if total else 0.0,
            "mean_strength": round(sum(with_evidence_scores) / total, 2) if total else 0.0,
        },
        "baseline": {
            "defensible": baseline_defensible,
            "defence_rate": round(baseline_defensible / total, 4) if total else 0.0,
            "mean_strength": round(sum(baseline_scores) / total, 2) if total else 0.0,
        },
        "improvement": round((defensible - baseline_defensible) / total, 4) if total else 0.0,
        "refund_recommended": refund_recommended,
        "disputes": entries,
    }
