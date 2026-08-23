"""The completion orchestrator.

This is where verification, the kernel, the model boundary, escalation, payment and evidence meet.
The ordering rules it enforces:

    - verification runs first and refuses at the first failed step
    - the kernel runs next; its refusals are final and no model is consulted after one
    - the model is consulted only on constraints the kernel marked unresolved, and can only deny
      or escalate
    - revocation is re-read immediately before capture
    - money moves only with a persisted approving verdict id, passed explicitly
    - a revocation that lands after capture triggers an automatic compensating refund, its own
      status, and an evidence packet regardless
    - an evidence packet is written on every path, including every refusal
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ap2.constraints import ConstraintOutcome
from app.ap2.jose import sign_jws
from app.ap2.vocabulary import (
    NATURAL_LANGUAGE_CONSTRAINT,
    RECEIPT_JWT_TYP,
    ReceiptStatus,
)
from app.catalog import policy_terms
from app.correlation import set_correlation_id
from app.db.base import utcnow
from app.db.models import (
    AgentIdentity,
    BudgetReservation,
    CheckoutSession,
    CheckoutState,
    EscalationStatus,
    OpenMandate,
    PaymentException,
    PaymentStatus,
    Product,
    RefundStatus,
    ReservationStatus,
)
from app.errors import status_for
from app.escalation import service as escalation_service
from app.escalation.whatsapp import WhatsAppTransport
from app.evidence import locker, packet
from app.kernel import budget, inventory, kernel, revocation, velocity
from app.kernel.reasons import Decision, ReasonCode
from app.kernel.verdict import KernelAction, allow, refuse
from app.kernel.verdict import record as record_verdict
from app.keys import merchant_key
from app.logging import get_logger
from app.payments import service as payments
from app.payments.gateway import GatewayError, PaymentGateway, get_gateway
from app.semantic.check import SemanticClient, SemanticOutcome, SemanticResult, evaluate_all
from app.settings import settings
from app.trust.registry import get_registry
from app.verification.pipeline import PresentedCredentials, VerifiedAuthority, verify

logger = get_logger(__name__)


@dataclass
class CompletionOutcome:
    status: str
    reason_code: ReasonCode
    # Left at zero so every branch gets the status its reason code maps to, rather than each
    # branch having to remember. A refusal that answered HTTP 200 would be unreadable to an agent.
    http_status: int = 0
    checkout_id: str | None = None
    verdict_id: str | None = None
    payment_id: str | None = None
    refund_id: str | None = None
    receipt: dict[str, Any] | None = None
    receipt_jwt: str | None = None
    challenge: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    evidence_packet_id: str | None = None

    def __post_init__(self) -> None:
        if self.http_status == 0:
            if self.status in ("completed", "compensated"):
                self.http_status = 200
            elif self.status == "awaiting_payment":
                self.http_status = 202
            else:
                self.http_status = status_for(self.reason_code)

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def _natural_language_constraints(authority: VerifiedAuthority) -> list[str]:
    texts: list[str] = []
    for result in authority.constraint_results:
        if (
            result.outcome is ConstraintOutcome.UNRESOLVED
            and result.constraint_type == NATURAL_LANGUAGE_CONSTRAINT
        ):
            text = str(result.detail.get("text", "")).strip()
            if text:
                texts.append(text)
    return texts


def _cart_items_for_model(session: Session, row: CheckoutSession) -> list[dict[str, Any]]:
    """Product data reaching the model is untrusted, and is passed as data with no privileges."""
    items: list[dict[str, Any]] = []
    for line in row.checkout.get("line_items", []) or []:
        sku = str((line.get("item") or {}).get("id", ""))
        product = session.scalar(select(Product).where(Product.sku == sku))
        items.append(
            {
                "sku": sku,
                "title": (line.get("item") or {}).get("title", ""),
                "quantity": line.get("quantity", 0),
                "category": product.category if product else "unknown",
                "description": product.description if product else "",
                "attributes": dict(product.attributes or {}) if product else {},
            }
        )
    return items


def _kernel_items(session: Session, row: CheckoutSession) -> list[kernel.CartItem]:
    items: list[kernel.CartItem] = []
    for line in row.checkout.get("line_items", []) or []:
        sku = str((line.get("item") or {}).get("id", ""))
        product = session.scalar(select(Product).where(Product.sku == sku))
        if product is None:
            continue
        items.append(
            kernel.CartItem(
                sku=sku,
                quantity=int(line.get("quantity", 0)),
                category=product.category,
                unit_price_minor=product.price_minor,
                min_order_quantity=product.min_order_quantity,
                max_order_quantity=product.max_order_quantity,
                age_restricted=product.age_restricted,
                region_lock=tuple(product.region_lock or []),
            )
        )
    return items


def _upsert_open_mandate(session: Session, authority: VerifiedAuthority) -> OpenMandate:
    """Record the open Checkout Mandate so budget, revocation and usage can be tracked."""
    existing = session.scalar(
        select(OpenMandate).where(OpenMandate.digest == authority.open_checkout_digest)
    )
    if existing is not None:
        return existing

    cap = None
    currency = authority.checkout.currency
    payment_constraints = (
        authority.open_payment_claims.get("constraints", [])
        if authority.open_payment_claims
        else []
    )
    for constraint in payment_constraints:
        if constraint.get("type") == "payment.amount_range" and constraint.get("max") is not None:
            cap = int(constraint["max"])
            currency = str(constraint.get("currency", currency))
        if constraint.get("type") == "payment.budget" and constraint.get("max") is not None:
            budget_cap = int(float(constraint["max"]))
            cap = budget_cap if cap is None else min(cap, budget_cap)

    row = OpenMandate(
        kind="checkout",
        digest=authority.open_checkout_digest,
        sd_jwt="",
        claims=authority.open_checkout_claims,
        agent_id=authority.agent_id,
        key_thumbprint=authority.key_thumbprint,
        issuer_id=authority.issuer_id,
        tier=authority.tier.name,
        cap_minor=cap,
        currency=currency,
        not_before=None,
        expires_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _receipt(
    *,
    status: ReceiptStatus,
    reference: str,
    order_id: str | None,
    error: str | None,
    description: str | None,
) -> tuple[dict[str, Any], str]:
    body: dict[str, Any] = {
        "status": status.value,
        "iss": settings.MERCHANT_ID,
        "iat": int(utcnow().timestamp()),
        "reference": reference,
    }
    if status is ReceiptStatus.SUCCESS:
        body["order_id"] = order_id or ""
    else:
        body["error"] = error or "REFUSED"
        body["error_description"] = description or "the checkout was refused"
    return body, sign_jws(body, merchant_key(), typ=RECEIPT_JWT_TYP)


def complete(
    session: Session,
    credentials: PresentedCredentials,
    *,
    correlation_id: str,
    semantic_client: SemanticClient | None = None,
    gateway: PaymentGateway | None = None,
    whatsapp: WhatsAppTransport | None = None,
    audience: str | None = None,
    buyer_region: str | None = None,
    authorize: bool = True,
) -> CompletionOutcome:
    """Drive a checkout attempt to a recorded outcome, whatever that outcome is."""
    # Every log line and stored record for this attempt carries the same identifier.
    set_correlation_id(correlation_id)
    timings = packet.TimingRecorder()
    semantic_evidence: list[dict[str, Any]] = []
    row: CheckoutSession | None = None
    agent_id = "unknown"

    def file_evidence(
        outcome: str, verification: dict[str, Any] | None, extra: dict[str, Any] | None = None
    ) -> str:
        body = packet.build(
            session,
            correlation_id=correlation_id,
            outcome=outcome,
            agent_id=agent_id,
            credentials=credentials.as_evidence(),
            verification=verification,
            checkout_row=row,
            semantic=semantic_evidence,
            timings=timings.as_list(),
            extra=extra,
        )
        return locker.append(session, correlation_id=correlation_id, body=body).packet_id

    # ---- verification -------------------------------------------------------------------------
    step = timings.start("verification")
    result = verify(session, credentials, audience=audience)
    timings.finish(step)

    if not result.ok:
        failure = result.failure
        assert failure is not None
        verdict = refuse(
            failure.reason_code,
            KernelAction.CHECKOUT,
            agent_id,
            correlation_id=correlation_id,
            evidence={"verification": failure.as_evidence()},
        )
        verdict_row = record_verdict(session, verdict)
        packet_id = file_evidence("refused_verification", failure.as_evidence())
        return CompletionOutcome(
            status="refused",
            reason_code=failure.reason_code,
            verdict_id=verdict_row.id,
            detail=failure.as_evidence(),
            evidence_packet_id=packet_id,
        )

    authority = result.authority
    assert authority is not None
    agent_id = authority.agent_id
    row = authority.session_row
    row.correlation_id = correlation_id
    mandate = _upsert_open_mandate(session, authority)
    row.mandate_id = mandate.id
    row.verified = True
    session.flush()

    agent = session.scalar(select(AgentIdentity).where(AgentIdentity.agent_id == agent_id))
    if agent is None:
        agent = AgentIdentity(
            agent_id=agent_id,
            display_name=agent_id[:60],
            key_thumbprint=authority.key_thumbprint,
            public_jwk=authority.agent_jwk,
            issuer_id=authority.issuer_id,
            tier=authority.tier.name,
        )
        session.add(agent)
        session.flush()
    else:
        agent.tier = authority.tier.name
        agent.issuer_id = authority.issuer_id

    # ---- kernel -------------------------------------------------------------------------------
    step = timings.start("policy_kernel")
    live_policy = policy_terms.active_terms(session).content_hash
    kernel_input = kernel.KernelInput(
        action=KernelAction.CHECKOUT,
        agent_id=agent_id,
        amount_minor=row.total_minor,
        currency=row.currency,
        tier=authority.tier,
        agent=agent,
        checkout_id=row.id,
        mandate_id=mandate.id,
        items=_kernel_items(session, row),
        constraint_results=authority.constraint_results,
        policy_hash_live=live_policy,
        policy_hash_acknowledged=authority.policy_hash,
        buyer_region=buyer_region,
        verified=True,
        correlation_id=correlation_id,
    )
    kernel_result = kernel.evaluate(session, kernel_input)
    timings.finish(step)

    if kernel_result.verdict.decision in (Decision.DENY, Decision.CHALLENGE):
        verdict_row = record_verdict(session, kernel_result.verdict)
        inventory.release(session, row.id)
        row.state = CheckoutState.REFUSED
        session.flush()
        packet_id = file_evidence("refused_kernel", authority.as_evidence())
        return CompletionOutcome(
            status="refused",
            reason_code=kernel_result.verdict.reason_code,
            http_status=status_for(kernel_result.verdict.reason_code),
            checkout_id=row.id,
            verdict_id=verdict_row.id,
            challenge=kernel_result.challenge,
            detail=kernel_result.verdict.evidence,
            evidence_packet_id=packet_id,
        )

    reservation_id = kernel_result.reservation_id

    # ---- the model boundary, only for what the kernel could not decide ------------------------
    escalation_required = kernel_result.escalates
    if kernel_result.escalates:
        texts = _natural_language_constraints(authority)
        if texts and semantic_client is not None:
            step = timings.start("semantic_check")
            results: list[SemanticResult] = evaluate_all(
                semantic_client,
                constraints=texts,
                items=_cart_items_for_model(session, row),
            )
            timings.finish(step)
            semantic_evidence = [r.as_evidence() for r in results]
            if any(r.outcome is SemanticOutcome.DENY for r in results):
                denial = next(r for r in results if r.outcome is SemanticOutcome.DENY)
                verdict = refuse(
                    ReasonCode.SEMANTIC_DENIED,
                    KernelAction.CHECKOUT,
                    agent_id,
                    correlation_id=correlation_id,
                    amount_minor=row.total_minor,
                    currency=row.currency,
                    checkout_id=row.id,
                    mandate_id=mandate.id,
                    evidence={"semantic": denial.as_evidence()},
                )
                verdict_row = record_verdict(session, verdict)
                if reservation_id:
                    budget.release(session, reservation_id)
                inventory.release(session, row.id)
                row.state = CheckoutState.REFUSED
                session.flush()
                packet_id = file_evidence("refused_semantic", authority.as_evidence())
                return CompletionOutcome(
                    status="refused",
                    reason_code=ReasonCode.SEMANTIC_DENIED,
                    checkout_id=row.id,
                    verdict_id=verdict_row.id,
                    detail=denial.as_evidence(),
                    evidence_packet_id=packet_id,
                )

    # ---- escalation ---------------------------------------------------------------------------
    if escalation_required:
        step = timings.start("escalation")
        constraint_text = "; ".join(_natural_language_constraints(authority)) or (
            "a constraint could not be decided mechanically"
        )
        existing = escalation_service.pending_for_checkout(session, row.id)
        if existing is None:
            existing = escalation_service.raise_escalation(
                session,
                correlation_id=correlation_id,
                checkout_id=row.id,
                agent_id=agent_id,
                constraint_text=constraint_text,
                raised_reason=ReasonCode.CONSTRAINT_UNRESOLVED.value,
                amount_minor=row.total_minor,
                currency=row.currency,
                fingerprint=row.cart_fingerprint,
                cart_summary=_cart_summary(row),
                transport=whatsapp,
            )
        settled = escalation_service.resolve(
            session, existing.id, current_fingerprint=row.cart_fingerprint
        )
        timings.finish(step)

        if settled.status != EscalationStatus.APPROVED:
            mapping = {
                EscalationStatus.PENDING: ReasonCode.ESCALATION_REQUIRED,
                EscalationStatus.DENIED: ReasonCode.ESCALATION_DENIED,
                EscalationStatus.TIMED_OUT: ReasonCode.ESCALATION_TIMEOUT,
                EscalationStatus.VOIDED: ReasonCode.ESCALATION_CART_CHANGED,
            }
            code = mapping.get(EscalationStatus(settled.status), ReasonCode.ESCALATION_REQUIRED)
            verdict = refuse(
                code,
                KernelAction.CHECKOUT,
                agent_id,
                decision=Decision.ESCALATE
                if code is ReasonCode.ESCALATION_REQUIRED
                else Decision.DENY,
                correlation_id=correlation_id,
                amount_minor=row.total_minor,
                currency=row.currency,
                checkout_id=row.id,
                mandate_id=mandate.id,
                evidence={"escalation": escalation_service.as_evidence(session, settled)},
            )
            verdict_row = record_verdict(session, verdict)
            if code is not ReasonCode.ESCALATION_REQUIRED:
                if reservation_id:
                    budget.release(session, reservation_id)
                inventory.release(session, row.id)
                row.state = CheckoutState.REFUSED
            session.flush()
            outcome_label = (
                "escalated" if code is ReasonCode.ESCALATION_REQUIRED else "refused_escalation"
            )
            packet_id = file_evidence(outcome_label, authority.as_evidence())
            return CompletionOutcome(
                status="escalated" if code is ReasonCode.ESCALATION_REQUIRED else "refused",
                reason_code=code,
                http_status=status_for(code),
                checkout_id=row.id,
                verdict_id=verdict_row.id,
                detail={
                    "escalation_id": settled.id,
                    "deadline_at": settled.deadline_at.isoformat(),
                },
                evidence_packet_id=packet_id,
            )

    # ---- revocation, read immediately before money moves --------------------------------------
    step = timings.start("revocation_precheck")
    revocation_state = revocation.check(session, mandate.id)
    timings.finish(step)
    if revocation_state.revoked:
        verdict = refuse(
            ReasonCode.MANDATE_REVOKED,
            KernelAction.CHECKOUT,
            agent_id,
            correlation_id=correlation_id,
            amount_minor=row.total_minor,
            currency=row.currency,
            checkout_id=row.id,
            mandate_id=mandate.id,
            evidence={"revocation": revocation_state.as_evidence()},
        )
        verdict_row = record_verdict(session, verdict)
        if reservation_id:
            budget.release(session, reservation_id)
        inventory.release(session, row.id)
        row.state = CheckoutState.REFUSED
        session.flush()
        packet_id = file_evidence("refused_revoked", authority.as_evidence())
        return CompletionOutcome(
            status="refused",
            reason_code=ReasonCode.MANDATE_REVOKED,
            checkout_id=row.id,
            verdict_id=verdict_row.id,
            detail=revocation_state.as_evidence(),
            evidence_packet_id=packet_id,
        )

    # ---- the approving verdict. Nothing above this line has moved money. ----------------------
    approval = kernel_result.verdict
    if escalation_required:
        approval = allow(
            ReasonCode.APPROVED_AFTER_HUMAN_APPROVAL,
            KernelAction.CHECKOUT,
            agent_id,
            correlation_id=correlation_id,
            amount_minor=row.total_minor,
            currency=row.currency,
            checkout_id=row.id,
            mandate_id=mandate.id,
            evidence={**approval.evidence, "human_approved": True},
        )
    verdict_row = record_verdict(session, approval)
    row.state = CheckoutState.COMPLETING
    session.flush()

    # ---- payment ------------------------------------------------------------------------------
    step = timings.start("payment")
    try:
        payment = payments.create_order(
            session,
            verdict_id=verdict_row.id,
            correlation_id=correlation_id,
            checkout_id=row.id,
            agent_id=agent_id,
            amount_minor=row.total_minor,
            currency=row.currency,
            gateway=gateway,
        )
        authorization = _authorize(gateway, payment) if authorize else None
        if authorization is not None:
            payments.attach_authorization(session, payment, authorization)
            payments.capture(
                session, verdict_id=verdict_row.id, payment=payment, gateway=gateway
            )
    except GatewayError as exc:
        timings.finish(step)
        if reservation_id:
            budget.release(session, reservation_id)
        inventory.release(session, row.id)
        row.state = CheckoutState.REFUSED
        failure_verdict = record_verdict(
            session,
            refuse(
                ReasonCode.PAYMENT_GATEWAY_ERROR,
                KernelAction.CAPTURE,
                agent_id,
                correlation_id=correlation_id,
                amount_minor=row.total_minor,
                currency=row.currency,
                checkout_id=row.id,
                mandate_id=mandate.id,
                evidence={"error": str(exc)},
            ),
        )
        session.flush()
        packet_id = file_evidence("payment_failed", authority.as_evidence(), {"error": str(exc)})
        return CompletionOutcome(
            status="refused",
            reason_code=ReasonCode.PAYMENT_GATEWAY_ERROR,
            http_status=502,
            checkout_id=row.id,
            verdict_id=failure_verdict.id,
            detail={"error": str(exc)},
            evidence_packet_id=packet_id,
        )
    timings.finish(step)

    if payment.status != PaymentStatus.CAPTURED:
        # The Credential Provider is out of scope and mocked, so in a live Razorpay run there
        # is nothing here that can pay the order. Reporting this as completed would be untrue,
        # so the order is returned for payment and the signed webhook finalises it. The budget
        # reservation stays held and expires on its own if the payment never arrives.
        row.state = CheckoutState.AWAITING_PAYMENT
        session.flush()
        packet_id = file_evidence("awaiting_payment", authority.as_evidence())
        return CompletionOutcome(
            status="awaiting_payment",
            reason_code=approval.reason_code,
            checkout_id=row.id,
            verdict_id=verdict_row.id,
            payment_id=payment.id,
            detail={
                "razorpay_order_id": payment.razorpay_order_id,
                "amount": {"amount": payment.amount_minor, "currency": payment.currency},
                "next": (
                    "the credential provider must pay this order; the merchant finalises "
                    "the checkout when the signed payment.captured webhook arrives"
                ),
            },
            evidence_packet_id=packet_id,
        )

    captured_at = payment.captured_at or utcnow()

    # ---- revocation that landed after capture -------------------------------------------------
    step = timings.start("revocation_postcheck")
    post = revocation.check(session, mandate.id)
    timings.finish(step)
    # Revocation that lands during or after capture is the graceful-failure case: the money has
    # already moved, so it must be given back automatically rather than merely refused.
    if post.revoked and payment.status == PaymentStatus.CAPTURED:
        compensation_verdict = record_verdict(
            session,
            refuse(
                ReasonCode.REVOKED_AFTER_CAPTURE_COMPENSATED,
                KernelAction.COMPENSATE,
                agent_id,
                correlation_id=correlation_id,
                amount_minor=row.total_minor,
                currency=row.currency,
                checkout_id=row.id,
                mandate_id=mandate.id,
                evidence={
                    "revocation": post.as_evidence(),
                    "captured_at": captured_at.isoformat(),
                    "landed_after_capture": revocation.revoked_after(post, captured_at),
                },
            ),
        )
        # The refund itself needs an approving verdict, so one is recorded for the refund action.
        refund_verdict = record_verdict(
            session,
            allow(
                ReasonCode.APPROVED,
                KernelAction.REFUND,
                agent_id,
                correlation_id=correlation_id,
                amount_minor=row.total_minor,
                currency=row.currency,
                checkout_id=row.id,
                mandate_id=mandate.id,
                evidence={"compensating_for": compensation_verdict.id},
            ),
        )
        refund_row = payments.refund(
            session,
            verdict_id=refund_verdict.id,
            payment=payment,
            reason="revocation_after_capture",
            compensating=True,
            gateway=gateway,
        )
        if reservation_id:
            budget.commit(session, reservation_id)
        inventory.restore(session, row.id)
        row.state = CheckoutState.COMPENSATED
        session.flush()
        packet_id = file_evidence("compensated", authority.as_evidence())
        return CompletionOutcome(
            status="compensated",
            reason_code=ReasonCode.REVOKED_AFTER_CAPTURE_COMPENSATED,
            checkout_id=row.id,
            verdict_id=compensation_verdict.id,
            payment_id=payment.id,
            refund_id=refund_row.id,
            detail={
                "refund": refund_row.razorpay_refund_id,
                "reason": "mandate revoked after capture",
            },
            evidence_packet_id=packet_id,
        )

    # ---- settle -------------------------------------------------------------------------------
    if reservation_id:
        budget.commit(session, reservation_id)
    velocity.record_spend(
        session,
        agent_id=agent_id,
        mandate_id=mandate.id,
        correlation_id=correlation_id,
        amount_minor=row.total_minor,
        currency=row.currency,
    )
    inventory.consume(session, row.id)
    row.state = CheckoutState.COMPLETED
    session.flush()

    payments.reconcile(session, payment, gateway=gateway)

    receipt_body, receipt_jwt = _receipt(
        status=ReceiptStatus.SUCCESS,
        reference=authority.closed_checkout_digest,
        order_id=payment.razorpay_order_id,
        error=None,
        description=None,
    )
    packet_id = file_evidence("completed", authority.as_evidence())
    return CompletionOutcome(
        status="completed",
        reason_code=approval.reason_code,
        checkout_id=row.id,
        verdict_id=verdict_row.id,
        payment_id=payment.id,
        receipt=receipt_body,
        receipt_jwt=receipt_jwt,
        evidence_packet_id=packet_id,
    )


def _compensate_after_capture(
    session: Session,
    row: CheckoutSession,
    payment: Any,
    reservation: Any,
    revoked: Any,
    gateway: PaymentGateway | None = None,
) -> str:
    """Give the money back automatically when a revocation lands after capture.

    The refund is attempted, not assumed. If the gateway refuses it, the checkout is not marked
    compensated: an exception is filed instead, so an operator sees that money was taken and not
    yet returned rather than the record quietly claiming otherwise.
    """
    compensation = record_verdict(
        session,
        refuse(
            ReasonCode.REVOKED_AFTER_CAPTURE_COMPENSATED,
            KernelAction.COMPENSATE,
            payment.agent_id,
            correlation_id=payment.correlation_id,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            checkout_id=row.id,
            mandate_id=row.mandate_id,
            evidence={"revocation": revoked.as_evidence(), "finalised_by": "webhook"},
        ),
    )
    refund_verdict = record_verdict(
        session,
        allow(
            ReasonCode.APPROVED,
            KernelAction.REFUND,
            payment.agent_id,
            correlation_id=payment.correlation_id,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            checkout_id=row.id,
            mandate_id=row.mandate_id,
            evidence={"compensating_for": compensation.id},
        ),
    )

    refunded = False
    failure: str | None = None
    try:
        refund_row = payments.refund(
            session,
            verdict_id=refund_verdict.id,
            payment=payment,
            reason="revocation_after_capture",
            compensating=True,
            gateway=gateway,
        )
        refunded = refund_row.status == RefundStatus.PROCESSED
    except GatewayError as exc:
        failure = str(exc)
        session.add(
            PaymentException(
                correlation_id=payment.correlation_id,
                payment_id=payment.id,
                kind="compensating_refund_failed",
                local_state={"checkout_id": row.id, "amount_minor": payment.amount_minor},
                gateway_state={"error": failure},
            )
        )

    if reservation is not None:
        budget.commit(session, reservation.id)
    inventory.restore(session, row.id)
    row.state = CheckoutState.COMPENSATED if refunded else CheckoutState.COMPLETING
    session.flush()

    body = packet.build(
        session,
        correlation_id=payment.correlation_id,
        outcome="compensated" if refunded else "compensation_failed",
        agent_id=payment.agent_id,
        credentials=None,
        verification=None,
        checkout_row=row,
        extra={
            "finalised_by": "razorpay payment.captured webhook",
            "revocation": revoked.as_evidence(),
            "refund_succeeded": refunded,
            "refund_error": failure,
        },
    )
    return locker.append(session, correlation_id=payment.correlation_id, body=body).packet_id


def finalise_captured(
    session: Session, payment: Any, *, gateway: PaymentGateway | None = None
) -> str | None:
    """Settle a checkout that was awaiting payment, once capture is confirmed.

    Driven by Razorpay's signed payment.captured webhook. It performs exactly the settlement the
    inline path performs, so a checkout finalised by webhook is indistinguishable from one
    finalised in the request that created it.
    """
    row = session.get(CheckoutSession, payment.checkout_id)
    if row is None or row.state not in (CheckoutState.AWAITING_PAYMENT, CheckoutState.COMPLETING):
        return None

    reservation = session.scalar(
        select(BudgetReservation).where(
            BudgetReservation.correlation_id == payment.correlation_id,
            BudgetReservation.status == ReservationStatus.RESERVED,
        )
    )

    # Revocation is re-read here as well as inline, because in a webhook-driven flow this is the
    # moment the money actually moves. A revocation that landed between authorisation and capture
    # must be compensated, not settled.
    revoked = revocation.check(session, row.mandate_id)
    if revoked.revoked:
        return _compensate_after_capture(session, row, payment, reservation, revoked, gateway)

    if reservation is not None:
        budget.commit(session, reservation.id)
    velocity.record_spend(
        session,
        agent_id=payment.agent_id,
        mandate_id=row.mandate_id,
        correlation_id=payment.correlation_id,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
    )
    inventory.consume(session, row.id)
    row.state = CheckoutState.COMPLETED
    session.flush()

    body = packet.build(
        session,
        correlation_id=payment.correlation_id,
        outcome="completed",
        agent_id=payment.agent_id,
        credentials=None,
        verification=None,
        checkout_row=row,
        extra={"finalised_by": "razorpay payment.captured webhook"},
    )
    return locker.append(session, correlation_id=payment.correlation_id, body=body).packet_id


def finalise_failed(
    session: Session, payment: Any, *, error: dict[str, Any] | None = None
) -> str | None:
    """Unwind a checkout whose payment the gateway has reported as failed.

    The reservation TTL would eventually free the budget, but that fallback exists for states
    Dwarpal cannot observe. Here the gateway has said the payment is dead, so holding the human's
    budget and the last unit of stock against it is a decision, and the wrong one.
    """
    row = session.get(CheckoutSession, payment.checkout_id)
    if row is None or row.state not in (CheckoutState.AWAITING_PAYMENT, CheckoutState.COMPLETING):
        return None

    budget.release_by_correlation(session, payment.correlation_id)
    inventory.release(session, row.id)
    row.state = CheckoutState.CANCELLED
    session.flush()

    body = packet.build(
        session,
        correlation_id=payment.correlation_id,
        outcome="payment_failed",
        agent_id=payment.agent_id,
        credentials=None,
        verification=None,
        checkout_row=row,
        extra={
            "finalised_by": "razorpay payment.failed webhook",
            "gateway_error": error or {},
        },
    )
    return locker.append(session, correlation_id=payment.correlation_id, body=body).packet_id


def _authorize(gateway: PaymentGateway | None, payment: Any) -> dict[str, Any] | None:
    """Ask the mocked Credential Provider to pay the order in test mode.

    The Credential Provider is out of scope for this project and is mocked, as the README states.
    The stub gateway implements the same handshake the real one would.
    """
    client = gateway or get_gateway()
    if hasattr(client, "authorize"):
        return client.authorize(str(payment.razorpay_order_id), amount_minor=payment.amount_minor)
    # The real Razorpay gateway has no server-side authorisation without S2S enabled, so the order
    # is left for the Credential Provider to pay.
    return None


def _cart_summary(row: CheckoutSession) -> str:
    parts = []
    for line in row.checkout.get("line_items", []) or []:
        item = line.get("item") or {}
        parts.append(f"{line.get('quantity')} x {item.get('title', item.get('id'))}")
    return ", ".join(parts)[:400]


def agent_tier_or_unverified(agent: AgentIdentity | None) -> Any:
    registry = get_registry()
    if agent is None:
        return registry.unverified()
    return registry.tiers.get(agent.tier, registry.unverified())
