"""Money paths: the ordering rule, idempotency, reconciliation, and compensation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.checkout import quote
from app.checkout.complete import complete
from app.db.models import (
    CheckoutState,
    Payment,
    PaymentException,
    PaymentStatus,
    Refund,
    RefundStatus,
)
from app.db.models import Verdict as VerdictRow
from app.harness import factory
from app.kernel import budget, revocation, velocity
from app.kernel.reasons import Decision, ReasonCode
from app.kernel.verdict import KernelAction, allow, record, refuse
from app.payments import reconcile as reconciliation
from app.payments import service as payments
from app.payments.gateway import GatewayError, StubGateway
from app.verification.pipeline import verify

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]


def _prepare(db, correlation: str, lines=None, spec=None):
    lines = lines or CART
    quoted = quote.create_quote(
        db,
        agent_id=f"agent:{correlation}",
        correlation_id=correlation,
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in lines],
    )
    principals = factory.Principals.create(agent_id=f"agent:{correlation}")
    presentation = factory.present(
        principals,
        spec or factory.spec_for_cart(lines),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce=f"nonce-{correlation}",
    )
    return quoted, presentation


# --- the ordering rule ---------------------------------------------------------------------------


def test_a_refusing_verdict_cannot_authorise_an_order(seeded, gateway):
    db = seeded
    denial = record(
        db,
        refuse(
            ReasonCode.BUDGET_EXCEEDED,
            KernelAction.CHECKOUT,
            "agent:x",
            correlation_id="dwc_deny",
            amount_minor=1000,
        ),
    )
    with pytest.raises(payments.VerdictMissing):
        payments.create_order(
            db,
            verdict_id=denial.id,
            correlation_id="dwc_deny",
            checkout_id="co_x",
            agent_id="agent:x",
            amount_minor=1000,
            currency="INR",
            gateway=gateway,
        )
    assert db.scalar(select(Payment)) is None


def test_a_verdict_that_does_not_exist_cannot_authorise_an_order(seeded, gateway):
    db = seeded
    with pytest.raises(payments.VerdictMissing):
        payments.create_order(
            db,
            verdict_id="no-such-verdict",
            correlation_id="dwc_ghost",
            checkout_id="co_x",
            agent_id="agent:x",
            amount_minor=1000,
            currency="INR",
            gateway=gateway,
        )


def test_a_verdict_cannot_be_constructed_with_a_mismatched_decision():
    with pytest.raises(ValueError):
        allow(ReasonCode.BUDGET_EXCEEDED, KernelAction.CHECKOUT, "agent:x")
    with pytest.raises(ValueError):
        refuse(ReasonCode.APPROVED, KernelAction.CHECKOUT, "agent:x")
    with pytest.raises(TypeError):
        allow("APPROVED", KernelAction.CHECKOUT, "agent:x")  # type: ignore[arg-type]


# --- idempotency ---------------------------------------------------------------------------------


def test_a_retried_completion_does_not_charge_twice(seeded, gateway):
    db = seeded
    _, presentation = _prepare(db, "dwc_retry")
    first = complete(db, presentation.credentials, correlation_id="dwc_retry", gateway=gateway)
    assert first.status == "completed"

    captures = [c for c in gateway.calls if c[0] == "capture_payment"]
    assert len(captures) == 1
    assert len(db.scalars(select(Payment)).all()) == 1


def test_create_order_is_idempotent_for_one_checkout(seeded, gateway):
    db = seeded
    quoted, _presentation = _prepare(db, "dwc_order_once")
    verdict = record(
        db,
        allow(
            ReasonCode.APPROVED,
            KernelAction.CHECKOUT,
            "agent:once",
            correlation_id="dwc_order_once",
            amount_minor=quoted.row.total_minor,
            checkout_id=quoted.row.id,
        ),
    )
    first = payments.create_order(
        db,
        verdict_id=verdict.id,
        correlation_id="dwc_order_once",
        checkout_id=quoted.row.id,
        agent_id="agent:once",
        amount_minor=quoted.row.total_minor,
        currency="INR",
        gateway=gateway,
    )
    second = payments.create_order(
        db,
        verdict_id=verdict.id,
        correlation_id="dwc_order_once",
        checkout_id=quoted.row.id,
        agent_id="agent:once",
        amount_minor=quoted.row.total_minor,
        currency="INR",
        gateway=gateway,
    )
    assert first.id == second.id
    assert len([c for c in gateway.calls if c[0] == "create_order"]) == 1


def test_capture_is_idempotent(seeded, gateway):
    db = seeded
    quoted, _ = _prepare(db, "dwc_capture_once")
    verdict = record(
        db,
        allow(
            ReasonCode.APPROVED,
            KernelAction.CAPTURE,
            "agent:cap",
            correlation_id="dwc_capture_once",
            amount_minor=quoted.row.total_minor,
            checkout_id=quoted.row.id,
        ),
    )
    payment = payments.create_order(
        db,
        verdict_id=verdict.id,
        correlation_id="dwc_capture_once",
        checkout_id=quoted.row.id,
        agent_id="agent:cap",
        amount_minor=quoted.row.total_minor,
        currency="INR",
        gateway=gateway,
    )
    payments.attach_authorization(db, payment, gateway.authorize(payment.razorpay_order_id))
    payments.capture(db, verdict_id=verdict.id, payment=payment, gateway=gateway)
    payments.capture(db, verdict_id=verdict.id, payment=payment, gateway=gateway)

    assert len([c for c in gateway.calls if c[0] == "capture_payment"]) == 1
    assert payment.status == PaymentStatus.CAPTURED


# --- revocation after capture ---------------------------------------------------------------------


def test_revocation_after_capture_compensates_automatically(seeded, gateway):
    """The graceful failure the project is judged on."""
    db = seeded
    _, presentation = _prepare(db, "dwc_after_capture")

    # Register the mandate, then arrange for the revocation to land while the flow is running.
    probe = verify(db, presentation.credentials, record_nonce=False)
    assert probe.ok, probe.failure
    from app.checkout.complete import _upsert_open_mandate

    mandate = _upsert_open_mandate(db, probe.authority)
    db.flush()

    original_capture = payments.capture

    def capture_then_revoke(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        revocation.revoke(db, mandate.id, "principal revoked while the capture was in flight")
        db.flush()
        return result

    payments.capture = capture_then_revoke
    try:
        outcome = complete(
            db, presentation.credentials, correlation_id="dwc_after_capture", gateway=gateway
        )
    finally:
        payments.capture = original_capture

    assert outcome.status == "compensated"
    assert outcome.reason_code is ReasonCode.REVOKED_AFTER_CAPTURE_COMPENSATED

    refund = db.scalar(select(Refund))
    assert refund is not None
    assert refund.compensating is True
    assert refund.status == RefundStatus.PROCESSED
    assert refund.reason == "revocation_after_capture"

    payment = db.get(Payment, outcome.payment_id)
    assert payment.amount_minor == refund.amount_minor

    from app.db.models import CheckoutSession

    row = db.scalar(select(CheckoutSession).where(CheckoutSession.id == outcome.checkout_id))
    assert row.state == CheckoutState.COMPENSATED

    # The evidence packet is filed even though the transaction was undone.
    from app.evidence import locker

    packets = locker.for_correlation(db, "dwc_after_capture")
    assert packets and packets[-1].body["outcome"] == "compensated"
    assert packets[-1].body["refunds"]


def test_compensating_refund_requires_its_own_approving_verdict(seeded, gateway):
    db = seeded
    _, presentation = _prepare(db, "dwc_refund_verdict")
    probe = verify(db, presentation.credentials, record_nonce=False)
    from app.checkout.complete import _upsert_open_mandate

    mandate = _upsert_open_mandate(db, probe.authority)
    db.flush()

    original_capture = payments.capture

    def capture_then_revoke(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        revocation.revoke(db, mandate.id, "revoked")
        db.flush()
        return result

    payments.capture = capture_then_revoke
    try:
        complete(db, presentation.credentials, correlation_id="dwc_refund_verdict", gateway=gateway)
    finally:
        payments.capture = original_capture

    refund_verdicts = [
        v
        for v in db.scalars(select(VerdictRow)).all()
        if v.action == KernelAction.REFUND and v.decision == Decision.ALLOW.value
    ]
    assert refund_verdicts, "a refund must be preceded by its own approving verdict"


# --- reconciliation --------------------------------------------------------------------------------


def test_reconciliation_records_a_discrepancy_rather_than_correcting_it(seeded):
    db = seeded
    gateway = StubGateway()
    quoted, _ = _prepare(db, "dwc_reconcile")
    verdict = record(
        db,
        allow(
            ReasonCode.APPROVED,
            KernelAction.CAPTURE,
            "agent:rec",
            correlation_id="dwc_reconcile",
            amount_minor=quoted.row.total_minor,
            checkout_id=quoted.row.id,
        ),
    )
    payment = payments.create_order(
        db,
        verdict_id=verdict.id,
        correlation_id="dwc_reconcile",
        checkout_id=quoted.row.id,
        agent_id="agent:rec",
        amount_minor=quoted.row.total_minor,
        currency="INR",
        gateway=gateway,
    )
    payments.attach_authorization(db, payment, gateway.authorize(payment.razorpay_order_id))
    payments.capture(db, verdict_id=verdict.id, payment=payment, gateway=gateway)

    # Razorpay now disagrees with the local record.
    gateway.payments[payment.razorpay_payment_id]["status"] = "failed"
    exception = reconciliation.reconcile(db, payment, gateway=gateway)

    assert exception is not None
    assert exception.kind == "reconciliation_discrepancy"
    assert exception.gateway_state["mismatches"]["status"]["gateway"] == "failed"
    # The local record is not silently rewritten. The disagreement itself is the evidence.
    assert payment.status == PaymentStatus.CAPTURED
    assert db.scalar(select(PaymentException)) is not None


def test_a_gateway_failure_releases_the_budget_and_the_stock(seeded):
    db = seeded
    gateway = StubGateway(fail_on={"create_order"})
    _, presentation = _prepare(db, "dwc_gateway_down")

    outcome = complete(
        db, presentation.credentials, correlation_id="dwc_gateway_down", gateway=gateway
    )
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.PAYMENT_GATEWAY_ERROR
    assert outcome.http_status == 502

    from app.db.models import BudgetReservation, ReservationStatus

    reservations = db.scalars(select(BudgetReservation)).all()
    assert all(r.status != ReservationStatus.RESERVED for r in reservations)


def test_refund_failure_is_recorded_and_raised(seeded):
    db = seeded
    gateway = StubGateway()
    quoted, _ = _prepare(db, "dwc_refund_fail")
    verdict = record(
        db,
        allow(
            ReasonCode.APPROVED,
            KernelAction.REFUND,
            "agent:rf",
            correlation_id="dwc_refund_fail",
            amount_minor=quoted.row.total_minor,
            checkout_id=quoted.row.id,
        ),
    )
    payment = payments.create_order(
        db,
        verdict_id=verdict.id,
        correlation_id="dwc_refund_fail",
        checkout_id=quoted.row.id,
        agent_id="agent:rf",
        amount_minor=quoted.row.total_minor,
        currency="INR",
        gateway=gateway,
    )
    payments.attach_authorization(db, payment, gateway.authorize(payment.razorpay_order_id))
    payments.capture(db, verdict_id=verdict.id, payment=payment, gateway=gateway)

    gateway.fail_on.add("create_refund")
    with pytest.raises(GatewayError):
        payments.refund(db, verdict_id=verdict.id, payment=payment, reason="test", gateway=gateway)

    row = db.scalar(select(Refund))
    assert row is not None
    assert row.status == RefundStatus.FAILED
    assert "error" in row.gateway_snapshot


# --- structuring and velocity ----------------------------------------------------------------------


def test_structuring_is_detected_across_the_window(seeded):
    db = seeded
    velocity.record_spend(
        db, agent_id="agent:s", mandate_id="m1", correlation_id="c1", amount_minor=40000
    )
    velocity.record_spend(
        db, agent_id="agent:s", mandate_id="m1", correlation_id="c2", amount_minor=40000
    )
    db.flush()

    finding = velocity.detect_structuring(
        db,
        agent_id="agent:s",
        mandate_id="m1",
        pending_amount_minor=40000,
        per_transaction_cap_minor=100000,
    )
    assert finding.detected is True
    assert finding.window_spend_minor == 120000
    assert finding.transaction_count == 3


def test_a_single_transaction_over_the_cap_is_not_called_structuring(seeded):
    db = seeded
    finding = velocity.detect_structuring(
        db,
        agent_id="agent:solo",
        mandate_id="m2",
        pending_amount_minor=500000,
        per_transaction_cap_minor=100000,
    )
    assert finding.detected is False, "one oversized transaction is a cap breach, not structuring"


def test_the_kill_switch_stops_one_agent_only(seeded, gateway):
    from app.db.models import AgentIdentity

    db = seeded
    db.add(
        AgentIdentity(
            agent_id="agent:dwc_killed",
            display_name="killed",
            key_thumbprint="tp",
            public_jwk={},
            issuer_id="did:web:trusted-surface.dwarpal.test",
            tier="accredited",
            kill_switch=True,
        )
    )
    db.flush()

    _, killed = _prepare(db, "dwc_killed")
    outcome = complete(db, killed.credentials, correlation_id="dwc_killed", gateway=gateway)
    assert outcome.reason_code is ReasonCode.AGENT_KILL_SWITCH

    _, healthy = _prepare(db, "dwc_healthy")
    other = complete(db, healthy.credentials, correlation_id="dwc_healthy", gateway=gateway)
    assert other.status == "completed", "one agent's kill switch must not affect another"


def test_budget_is_committed_only_after_the_money_moves(seeded, gateway):
    db = seeded
    _, presentation = _prepare(db, "dwc_commit")
    outcome = complete(db, presentation.credentials, correlation_id="dwc_commit", gateway=gateway)
    assert outcome.status == "completed"

    from app.db.models import OpenMandate

    mandate = db.scalar(select(OpenMandate))
    assert mandate.committed_minor == db.get(Payment, outcome.payment_id).amount_minor
    assert mandate.use_count == 1
    assert budget.state(db, mandate.id).reserved_minor == 0


# --- webhook-driven settlement ----------------------------------------------------------------


def _awaiting(db, gateway, correlation: str):
    """Drive a checkout to the awaiting-payment state, as a live Razorpay run would."""
    from app.checkout import complete as complete_module

    _, presentation = _prepare(db, correlation)
    original = complete_module._authorize
    complete_module._authorize = lambda *_a, **_k: None
    try:
        outcome = complete(db, presentation.credentials, correlation_id=correlation, gateway=gateway)
    finally:
        complete_module._authorize = original
    return outcome


def test_an_unpaid_order_is_not_reported_as_completed(seeded, gateway):
    db = seeded
    outcome = _awaiting(db, gateway, "dwc_awaiting")

    assert outcome.status == "awaiting_payment"
    assert outcome.http_status == 202
    assert outcome.detail["razorpay_order_id"]

    from app.db.models import CheckoutSession

    row = db.get(CheckoutSession, outcome.checkout_id)
    assert row.state == CheckoutState.AWAITING_PAYMENT
    payment = db.get(Payment, outcome.payment_id)
    assert payment.status == PaymentStatus.CREATED


def test_the_capture_webhook_finalises_the_checkout(seeded, gateway):
    from app.checkout.complete import finalise_captured
    from app.db.base import utcnow
    from app.db.models import CheckoutSession, OpenMandate

    db = seeded
    outcome = _awaiting(db, gateway, "dwc_webhook_settle")
    payment = db.get(Payment, outcome.payment_id)

    payment.razorpay_payment_id = "pay_webhook_1"
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    db.flush()

    packet_id = finalise_captured(db, payment)
    assert packet_id

    row = db.get(CheckoutSession, outcome.checkout_id)
    assert row.state == CheckoutState.COMPLETED
    mandate = db.scalar(select(OpenMandate))
    assert mandate.committed_minor == payment.amount_minor

    from app.evidence import locker

    packets = locker.for_correlation(db, "dwc_webhook_settle")
    assert packets[-1].body["outcome"] == "completed"
    assert packets[-1].body["extra"]["finalised_by"].endswith("webhook")


def test_a_failed_payment_releases_the_budget_and_the_stock(seeded, gateway):
    """A gateway-confirmed failure must unwind, not wait for the reservation TTL.

    The TTL exists for states Dwarpal cannot observe. Once Razorpay has said the payment is dead,
    continuing to hold the human's budget and the last unit of stock against it is a decision, and
    the wrong one: the agent's remaining authority is silently reduced and the stock is unsellable.
    """
    from app.checkout.complete import finalise_failed
    from app.db.models import BudgetReservation, CheckoutSession, ReservationStatus
    from app.evidence import locker

    db = seeded
    outcome = _awaiting(db, gateway, "dwc_payment_failed")
    payment = db.get(Payment, outcome.payment_id)

    reserved = db.scalars(
        select(BudgetReservation).where(
            BudgetReservation.correlation_id == "dwc_payment_failed",
            BudgetReservation.status == ReservationStatus.RESERVED,
        )
    ).all()
    assert reserved, "the awaiting-payment path should hold a reservation"

    payment.status = PaymentStatus.FAILED
    db.flush()
    packet_id = finalise_failed(
        db, payment, error={"error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_timeout"}
    )
    assert packet_id

    still_held = db.scalars(
        select(BudgetReservation).where(
            BudgetReservation.correlation_id == "dwc_payment_failed",
            BudgetReservation.status == ReservationStatus.RESERVED,
        )
    ).all()
    assert not still_held, "a failed payment must not keep the human's budget reserved"

    row = db.get(CheckoutSession, outcome.checkout_id)
    assert row.state == CheckoutState.CANCELLED

    body = locker.for_correlation(db, "dwc_payment_failed")[-1].body
    assert body["outcome"] == "payment_failed"
    assert body["extra"]["gateway_error"]["error_reason"] == "payment_timeout"

    # A replayed webhook must not unwind twice.
    assert finalise_failed(db, payment) is None


def test_a_webhook_settled_checkout_is_still_defensible(seeded, gateway):
    """The closing packet must carry the authority proved in the request that opened the checkout.

    A real Razorpay flow always settles in a later request than the one that verified the
    credentials. A dispute reads the closing packet, so if the chain were not carried forward
    every live transaction would look unauthorised and be recommended for refund.
    """
    from app.checkout.complete import finalise_captured
    from app.db.base import utcnow
    from app.disputes import responder
    from app.evidence import locker

    db = seeded
    outcome = _awaiting(db, gateway, "dwc_webhook_defence")
    payment = db.get(Payment, outcome.payment_id)
    payment.razorpay_payment_id = "pay_webhook_2"
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    db.flush()
    finalise_captured(db, payment)

    closing = locker.for_correlation(db, "dwc_webhook_defence")[-1].body
    assert closing["credential_chain"], "closing packet lost the credential chain"
    assert closing["verification"]["steps_passed"], "closing packet lost the verification record"

    dispute = responder.respond(
        db, correlation_id="dwc_webhook_defence", claim="the agent was never authorised"
    )
    assert dispute.recommendation == responder.Recommendation.CONTEST.value
    assert dispute.strength_score >= responder.CONTEST_THRESHOLD


def test_a_revocation_before_the_capture_webhook_compensates(seeded, gateway):
    """The webhook is where the money moves in a live run, so revocation is re-read there."""
    from app.checkout.complete import _upsert_open_mandate, finalise_captured
    from app.db.base import utcnow
    from app.db.models import CheckoutSession

    db = seeded
    _, presentation = _prepare(db, "dwc_webhook_revoked")
    probe = verify(db, presentation.credentials, record_nonce=False)
    mandate = _upsert_open_mandate(db, probe.authority)
    db.flush()

    from app.checkout import complete as complete_module

    original = complete_module._authorize
    complete_module._authorize = lambda *_a, **_k: None
    try:
        outcome = complete(
            db, presentation.credentials, correlation_id="dwc_webhook_revoked", gateway=gateway
        )
    finally:
        complete_module._authorize = original
    assert outcome.status == "awaiting_payment"

    # The human revokes while the payment is in flight.
    revocation.revoke(db, mandate.id, "revoked before the capture webhook arrived")
    db.flush()

    payment = db.get(Payment, outcome.payment_id)
    payment.razorpay_payment_id = gateway.authorize(payment.razorpay_order_id)["id"]
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    db.flush()

    packet_id = finalise_captured(db, payment, gateway=gateway)
    assert packet_id

    refund = db.scalar(select(Refund))
    assert refund is not None
    assert refund.compensating is True
    assert refund.status == RefundStatus.PROCESSED

    row = db.get(CheckoutSession, outcome.checkout_id)
    assert row.state == CheckoutState.COMPENSATED

    from app.evidence import locker

    body = locker.for_correlation(db, "dwc_webhook_revoked")[-1].body
    assert body["outcome"] == "compensated"
    assert body["extra"]["refund_succeeded"] is True


def test_a_failed_compensating_refund_is_not_reported_as_compensated(seeded, gateway):
    """If the money was not actually returned, the record must not claim it was."""
    from app.checkout.complete import _upsert_open_mandate, finalise_captured
    from app.db.base import utcnow
    from app.db.models import CheckoutSession

    db = seeded
    _, presentation = _prepare(db, "dwc_refund_blocked")
    probe = verify(db, presentation.credentials, record_nonce=False)
    mandate = _upsert_open_mandate(db, probe.authority)
    db.flush()

    from app.checkout import complete as complete_module

    original = complete_module._authorize
    complete_module._authorize = lambda *_a, **_k: None
    try:
        outcome = complete(
            db, presentation.credentials, correlation_id="dwc_refund_blocked", gateway=gateway
        )
    finally:
        complete_module._authorize = original

    revocation.revoke(db, mandate.id, "revoked")
    db.flush()

    payment = db.get(Payment, outcome.payment_id)
    payment.razorpay_payment_id = gateway.authorize(payment.razorpay_order_id)["id"]
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    db.flush()

    gateway.fail_on.add("create_refund")
    finalise_captured(db, payment, gateway=gateway)

    row = db.get(CheckoutSession, outcome.checkout_id)
    assert row.state != CheckoutState.COMPENSATED, (
        "a checkout must not be marked compensated when the refund did not go through"
    )
    exception = db.scalar(
        select(PaymentException).where(PaymentException.kind == "compensating_refund_failed")
    )
    assert exception is not None


def test_a_checkout_whose_hold_has_lapsed_is_not_sold(seeded, gateway):
    """Stock is held at quote, and the hold is what makes the sale real.

    A refusal releases the holds and the TTL expires them, and neither puts the Checkout into a
    state that refuses a later presentation. Selling then would take payment for stock nobody
    reserved, and the consume step would find no holds to decrement, so the shelf would not move
    and the same unit could be sold again.
    """
    from app.db.models import Product
    from app.kernel import inventory

    db = seeded
    quoted, presentation = _prepare(db, "dwc_lapsed_hold")
    product = db.scalar(select(Product).where(Product.sku == CART[0][0]))
    before = product.stock_total

    inventory.release(db, quoted.row.id)
    db.flush()

    outcome = complete(
        db, presentation.credentials, correlation_id="dwc_lapsed_hold", gateway=gateway
    )

    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.INVENTORY_UNAVAILABLE
    assert [c for c in gateway.calls if c[0] == "capture_payment"] == []
    db.refresh(product)
    assert product.stock_total == before
