"""The human-not-present purchase path, end to end, and the ways it must refuse."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.catalog import policy_terms
from app.checkout import quote
from app.checkout.complete import complete
from app.db.base import utcnow
from app.db.models import (
    CheckoutState,
    EvidencePacket,
    OpenMandate,
    Payment,
    PaymentStatus,
)
from app.db.models import Verdict as VerdictRow
from app.evidence import locker
from app.harness import factory
from app.kernel import revocation
from app.kernel.reasons import ReasonCode
from app.semantic.check import SemanticReply, Verdict
from app.semantic.client import StaticSemanticClient

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 2)]


def make_quote(db, agent_id="agent:dwarpal-reference-shopper", lines=None, correlation="dwc_test"):
    requested = [
        quote.RequestedLine(sku=sku, quantity=qty) for sku, _title, qty in (lines or CART)
    ]
    return quote.create_quote(
        db, agent_id=agent_id, correlation_id=correlation, lines=requested
    )


def present_for(db, quoted, *, spec=None, tamper=None, principals=None, lines=None):
    principals = principals or factory.Principals.create()
    spec = spec or factory.spec_for_cart(lines or CART)
    return principals, factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        tamper=tamper,
    )


def test_full_human_not_present_purchase_completes(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)

    outcome = complete(
        db, presentation.credentials, correlation_id="dwc_happy", gateway=gateway
    )

    assert outcome.status == "completed", outcome.detail
    assert outcome.reason_code is ReasonCode.APPROVED
    assert outcome.receipt is not None
    assert outcome.receipt["status"] == "Success"
    assert outcome.receipt_jwt

    payment = db.get(Payment, outcome.payment_id)
    assert payment is not None
    assert payment.status == PaymentStatus.CAPTURED
    assert payment.razorpay_order_id and payment.razorpay_payment_id

    row = db.get(type(quoted.row), quoted.row.id)
    assert row.state == CheckoutState.COMPLETED


def test_money_never_moves_without_a_verdict_recorded_first(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)
    outcome = complete(db, presentation.credentials, correlation_id="dwc_order", gateway=gateway)

    payment = db.get(Payment, outcome.payment_id)
    verdict = db.get(VerdictRow, payment.verdict_id)
    assert verdict is not None
    assert verdict.decision == "allow"
    # The verdict must predate the payment row it authorised.
    assert verdict.created_at <= payment.created_at


def test_verdict_is_signed_and_carries_a_reason_code(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)
    complete(db, presentation.credentials, correlation_id="dwc_signed", gateway=gateway)

    verdicts = list(db.scalars(select(VerdictRow)).all())
    assert verdicts
    for verdict in verdicts:
        assert verdict.reason_code
        assert verdict.signed_jwt.count(".") == 2


def test_every_path_files_an_evidence_packet(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)
    outcome = complete(db, presentation.credentials, correlation_id="dwc_evidence", gateway=gateway)

    packets = locker.for_correlation(db, "dwc_evidence")
    assert len(packets) == 1
    body = packets[0].body
    assert body["outcome"] == "completed"
    assert body["credential_chain"]["open_checkout_mandate"]
    assert body["checkout"]["catalog_snapshot"]
    assert body["checkout"]["policy_hash"]
    assert body["verdicts"]
    assert body["payments"]
    assert body["timings"]
    assert outcome.evidence_packet_id == packets[0].packet_id


def test_refusals_are_filed_as_evidence_too(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    # A cart the mandate does not authorise at all.
    spec = factory.spec_for_cart([("DWP-KBD-008", "Mechanical Keyboard 75 percent", 1)])
    _, presentation = present_for(db, quoted, spec=spec)

    outcome = complete(db, presentation.credentials, correlation_id="dwc_refused", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CONSTRAINT_LINE_ITEM_UNSATISFIED

    packets = locker.for_correlation(db, "dwc_refused")
    assert len(packets) == 1
    assert packets[0].body["outcome"] == "refused_kernel"


def test_replayed_credential_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)

    first = complete(db, presentation.credentials, correlation_id="dwc_first", gateway=gateway)
    assert first.status == "completed"

    second = complete(db, presentation.credentials, correlation_id="dwc_replay", gateway=gateway)
    assert second.status == "refused"
    assert second.reason_code is ReasonCode.CRED_REPLAYED


def test_confused_deputy_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted, tamper=factory.Tamper(wrong_agent_key=True))

    outcome = complete(db, presentation.credentials, correlation_id="dwc_deputy", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CRED_SUBJECT_MISMATCH


def test_forged_issuer_signature_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted, tamper=factory.Tamper(forge_issuer_signature=True))

    outcome = complete(db, presentation.credentials, correlation_id="dwc_forged", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CRED_SIGNATURE_INVALID


def test_expired_credential_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted, tamper=factory.Tamper(expired=True))

    outcome = complete(db, presentation.credentials, correlation_id="dwc_expired", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CRED_EXPIRED


def test_credential_valid_only_under_exaggerated_skew_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted, tamper=factory.Tamper(not_yet_valid=True))

    outcome = complete(db, presentation.credentials, correlation_id="dwc_skew", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CRED_NOT_YET_VALID


def test_unknown_issuing_authority_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted, tamper=factory.Tamper(unknown_issuer=True))

    outcome = complete(db, presentation.credentials, correlation_id="dwc_issuer", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CRED_ISSUER_UNKNOWN


def test_substituting_a_different_signed_checkout_is_refused(seeded, gateway):
    """The agent swaps in a genuinely merchant-signed Checkout belonging to another cart."""
    db = seeded
    quoted = make_quote(db)
    other = make_quote(db, correlation="dwc_other", lines=[("DWP-NTB-011", "Notebook", 1)])
    _, presentation = present_for(
        db,
        quoted,
        tamper=factory.Tamper(
            altered_checkout_jwt=other.checkout_jwt, altered_checkout_hash=other.checkout_hash
        ),
    )

    outcome = complete(db, presentation.credentials, correlation_id="dwc_altered", gateway=gateway)
    assert outcome.status == "refused"
    # The Payment Mandate authorises the original cart's total, so the substitution shows up as a
    # price mismatch against the Checkout actually presented.
    assert outcome.reason_code is ReasonCode.PRICE_DRIFT
    assert outcome.detail["detail"]["mandate_minor"] != outcome.detail["detail"]["checkout_minor"]


def test_cart_altered_after_the_merchant_signed_is_refused(seeded, gateway):
    """The agent edits the signed Checkout payload and re-hashes it to match."""
    import json

    from app.ap2.jose import b64url_decode, b64url_encode, canonical_json, sha256_b64url

    db = seeded
    quoted = make_quote(db)

    header, payload, signature = quoted.checkout_jwt.split(".")
    body = json.loads(b64url_decode(payload))
    # Quietly double the quantity the merchant committed to.
    body["checkout"]["line_items"][0]["quantity"] += 2
    forged_jwt = f"{header}.{b64url_encode(canonical_json(body))}.{signature}"
    forged_hash = sha256_b64url(forged_jwt.encode("ascii"))

    _, presentation = present_for(
        db,
        quoted,
        tamper=factory.Tamper(
            altered_checkout_jwt=forged_jwt, altered_checkout_hash=forged_hash
        ),
    )

    outcome = complete(db, presentation.credentials, correlation_id="dwc_tamper", gateway=gateway)
    assert outcome.status == "refused"
    # The hash is internally consistent, so the refusal comes from the merchant signature.
    assert outcome.reason_code is ReasonCode.CHECKOUT_BINDING_MISMATCH
    assert db.scalar(select(Payment)) is None


def test_checkout_hash_that_does_not_cover_its_jwt_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(
        db, quoted, tamper=factory.Tamper(altered_checkout_hash="not-the-real-hash")
    )

    outcome = complete(db, presentation.credentials, correlation_id="dwc_bind", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CHECKOUT_BINDING_MISMATCH


def test_amount_cap_breach_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    spec = factory.spec_for_cart(CART, amount_cap_minor=1000)
    _, presentation = present_for(db, quoted, spec=spec)

    outcome = complete(db, presentation.credentials, correlation_id="dwc_cap", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CONSTRAINT_AMOUNT_EXCEEDED


def test_price_altered_between_quote_and_payment_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    principals = factory.Principals.create()
    spec = factory.spec_for_cart(CART)
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        tamper=factory.Tamper(payment_amount_minor=1),
    )

    outcome = complete(db, presentation.credentials, correlation_id="dwc_drift", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.PRICE_DRIFT


def test_restricted_category_is_refused_for_an_insufficient_tier(seeded, gateway):
    db = seeded
    lines = [("DWP-WIN-005", "Sula Cabernet Shiraz 750ml", 1)]
    quoted = make_quote(db, lines=lines, correlation="dwc_wine")
    principals = factory.Principals.create(issuer_id=factory.SANDBOX_ISSUER)
    spec = factory.spec_for_cart(lines)
    _, presentation = present_for(db, quoted, spec=spec, principals=principals, lines=lines)

    outcome = complete(db, presentation.credentials, correlation_id="dwc_wine", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code in (
        ReasonCode.ITEM_AGE_RESTRICTED,
        ReasonCode.CATEGORY_FORBIDDEN,
    )


def test_policy_hash_mismatch_is_refused(seeded, gateway, monkeypatch):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)

    # The terms change between the quote and the checkout attempt.
    monkeypatch.setattr(
        policy_terms, "read_terms_file", lambda: "# Revised terms\n\nEverything has changed.\n"
    )
    policy_terms.ensure_active_terms(db)
    db.flush()

    outcome = complete(db, presentation.credentials, correlation_id="dwc_policy", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.POLICY_HASH_MISMATCH


def test_revocation_before_capture_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)

    # Register the mandate by running verification once without recording nonces, then revoke it.
    from app.verification.pipeline import verify

    result = verify(db, presentation.credentials, record_nonce=False)
    assert result.ok, result.failure
    from app.checkout.complete import _upsert_open_mandate

    mandate = _upsert_open_mandate(db, result.authority)
    revocation.revoke(db, mandate.id, "human changed their mind")
    db.flush()

    outcome = complete(db, presentation.credentials, correlation_id="dwc_revoked", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.MANDATE_REVOKED
    assert db.scalar(select(Payment)) is None


def test_natural_language_constraint_denied_by_the_model(seeded, gateway):
    db = seeded
    lines = [("DWP-MLK-003", "Fresh Paneer 400g", 1)]
    quoted = make_quote(db, lines=lines, correlation="dwc_nl")
    spec = factory.spec_for_cart(lines, natural_language=["nothing perishable"])
    _, presentation = present_for(db, quoted, spec=spec, lines=lines)

    client = StaticSemanticClient(
        SemanticReply(verdict=Verdict.VIOLATES, rationale="paneer is perishable")
    )
    outcome = complete(
        db,
        presentation.credentials,
        correlation_id="dwc_nl",
        gateway=gateway,
        semantic_client=client,
    )
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.SEMANTIC_DENIED
    assert db.scalar(select(Payment)) is None


def test_model_finding_no_violation_escalates_rather_than_approving(seeded, gateway, whatsapp):
    db = seeded
    lines = [("DWP-NTB-011", "Hardcover Notebook A5", 1)]
    quoted = make_quote(db, lines=lines, correlation="dwc_esc")
    spec = factory.spec_for_cart(lines, natural_language=["nothing perishable"])
    _, presentation = present_for(db, quoted, spec=spec, lines=lines)

    client = StaticSemanticClient(
        SemanticReply(verdict=Verdict.NO_VIOLATION_FOUND, rationale="a notebook is not perishable")
    )
    outcome = complete(
        db,
        presentation.credentials,
        correlation_id="dwc_esc",
        gateway=gateway,
        semantic_client=client,
        whatsapp=whatsapp,
    )
    assert outcome.status == "escalated"
    assert outcome.reason_code is ReasonCode.ESCALATION_REQUIRED
    assert db.scalar(select(Payment)) is None
    assert whatsapp.sent, "the human should have been contacted"


def test_unanswered_escalation_becomes_a_denial(seeded, gateway, whatsapp, monkeypatch):
    db = seeded
    lines = [("DWP-NTB-011", "Hardcover Notebook A5", 1)]
    quoted = make_quote(db, lines=lines, correlation="dwc_timeout")
    spec = factory.spec_for_cart(lines, natural_language=["nothing perishable"])
    _, presentation = present_for(db, quoted, spec=spec, lines=lines)

    client = StaticSemanticClient(SemanticReply(verdict=Verdict.NO_VIOLATION_FOUND))
    monkeypatch.setattr("app.settings.settings.ESCALATION_DEADLINE_SECONDS", 0)

    outcome = complete(
        db,
        presentation.credentials,
        correlation_id="dwc_timeout",
        gateway=gateway,
        semantic_client=client,
        whatsapp=whatsapp,
    )
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.ESCALATION_TIMEOUT
    assert db.scalar(select(Payment)) is None


def test_quote_holds_stock_and_completion_consumes_it(seeded, gateway):
    from app.catalog import service as catalog

    db = seeded
    # Read scalars, not ORM objects: the identity map would otherwise hand back the same row.
    before_stock = catalog.by_sku(db, "DWP-TEA-001").product.stock_total
    before_available = catalog.by_sku(db, "DWP-TEA-001").available

    quoted = make_quote(db)
    # A hold reduces availability without touching real stock.
    assert catalog.by_sku(db, "DWP-TEA-001").available == before_available - 2
    assert catalog.by_sku(db, "DWP-TEA-001").product.stock_total == before_stock

    _, presentation = present_for(db, quoted)
    complete(db, presentation.credentials, correlation_id="dwc_stock", gateway=gateway)

    # Completion converts the hold into a sale.
    assert catalog.by_sku(db, "DWP-TEA-001").product.stock_total == before_stock - 2


def test_expired_quote_is_refused(seeded, gateway):
    db = seeded
    quoted = make_quote(db)
    _, presentation = present_for(db, quoted)
    quoted.row.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    outcome = complete(db, presentation.credentials, correlation_id="dwc_stale", gateway=gateway)
    assert outcome.status == "refused"
    assert outcome.reason_code is ReasonCode.CHECKOUT_EXPIRED


def test_evidence_chain_verifies_after_a_batch_of_transactions(seeded, gateway):
    db = seeded
    for index in range(3):
        quoted = make_quote(db, correlation=f"dwc_batch_{index}")
        _, presentation = present_for(db, quoted)
        complete(db, presentation.credentials, correlation_id=f"dwc_batch_{index}", gateway=gateway)

    report = locker.verify_chain(db)
    assert report["packets"] == 3
    assert report["valid"], report["problems"]
    assert db.scalar(select(EvidencePacket).where(EvidencePacket.seq == 1)).prev_hash == "GENESIS"


@pytest.mark.parametrize("attempts", [2, 3])
def test_open_mandate_is_recorded_once_per_credential(seeded, gateway, attempts):
    db = seeded
    principals = factory.Principals.create()
    spec = factory.spec_for_cart(CART)
    for index in range(attempts):
        quoted = make_quote(db, correlation=f"dwc_m{index}")
        presentation = factory.present(
            principals,
            spec,
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"nonce-{index}",
        )
        complete(db, presentation.credentials, correlation_id=f"dwc_m{index}", gateway=gateway)

    # Each presentation mints a fresh open mandate token, so digests differ; what must not happen
    # is the same digest being recorded twice.
    digests = [m.digest for m in db.scalars(select(OpenMandate)).all()]
    assert len(digests) == len(set(digests))
