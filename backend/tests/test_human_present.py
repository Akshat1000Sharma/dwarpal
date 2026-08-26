"""The human-present flow, and the fact that being present buys nothing.

AP2's human-not-present flow is where the merchant's verification duty is hardest, and it is what
the rest of this suite is about. This module covers the other flow: a person is at a trusted
surface, and the surface says so.

The property under test is that presence is a credential rather than a courtesy. It is signed by an
authority the registry knows, bound to one Checkout, valid for a short window, and usable once. It
is recorded on the verdict and in the evidence packet, and it moves no limit: every case below that
asserts a refusal would be refused identically without it.
"""

from __future__ import annotations

import pytest

from app.checkout import quote
from app.checkout.complete import complete
from app.escalation import service as escalation_service
from app.escalation.whatsapp import RecordingTransport
from app.evidence import locker
from app.harness import factory
from app.kernel.reasons import ReasonCode
from app.payments.gateway import StubGateway
from app.semantic.client import KeywordSemanticClient
from app.settings import settings
from app.verification.pipeline import verify

TEA = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]
HEADPHONES = [("DWP-HDP-007", "Wireless Headphones", 1)]


def _quote(session, principals, cart, correlation):
    return quote.create_quote(
        session,
        agent_id=principals.agent_id,
        correlation_id=correlation,
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in cart],
    )


def _attempt(
    session,
    *,
    cart=TEA,
    human_present=True,
    tamper=None,
    amount_cap_minor=5_000_000,
    natural_language=None,
    label="present",
):
    principals = factory.Principals.create(agent_id=f"agent:{label}", register=True)
    quoted = _quote(session, principals, cart, f"corr_{label}")
    spec = factory.spec_for_cart(
        cart, amount_cap_minor=amount_cap_minor, natural_language=natural_language or []
    )
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce=f"nonce-{label}",
        tamper=tamper,
        human_present=human_present,
    )
    outcome = complete(
        session,
        presentation.credentials,
        correlation_id=f"corr_{label}",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    return principals, quoted, presentation, outcome


def test_a_present_human_completes_a_purchase(seeded):
    _p, _q, _pres, outcome = _attempt(seeded, label="happy")
    assert outcome.status in ("completed", "awaiting_payment")
    assert outcome.reason_code is ReasonCode.APPROVED


def test_presence_is_recorded_on_the_verified_authority(seeded):
    principals = factory.Principals.create(agent_id="agent:record", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_record")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(TEA),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-record",
        human_present=True,
    )
    result = verify(seeded, presentation.credentials, audience=settings.PUBLIC_BASE_URL)
    assert result.ok
    assert result.authority.human_present is True
    assert result.authority.presence_digest
    assert "presence_attestation" in result.authority.steps_passed
    evidence = result.authority.as_evidence()
    assert evidence["human_present"] is True
    assert evidence["presence"]["method"] == "surface_confirmation"


def test_the_flow_without_an_attestation_is_unchanged(seeded):
    principals = factory.Principals.create(agent_id="agent:absent", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_absent")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(TEA),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-absent",
        human_present=False,
    )
    assert presentation.credentials.presence is None
    result = verify(seeded, presentation.credentials, audience=settings.PUBLIC_BASE_URL)
    assert result.ok
    assert result.authority.human_present is False
    assert "presence_attestation" not in result.authority.steps_passed


@pytest.mark.parametrize(
    ("label", "tamper", "expected"),
    [
        (
            "stale",
            factory.Tamper(presence_age_seconds=3600),
            ReasonCode.PRESENCE_ATTESTATION_STALE,
        ),
        (
            "othercart",
            factory.Tamper(presence_checkout_hash="a-different-checkout-entirely"),
            ReasonCode.PRESENCE_BINDING_MISMATCH,
        ),
        (
            "untrusted",
            factory.Tamper(presence_issuer_id=factory.UNKNOWN_ISSUER),
            ReasonCode.PRESENCE_ISSUER_UNTRUSTED,
        ),
        (
            "forged",
            factory.Tamper(forge_presence_signature=True),
            ReasonCode.PRESENCE_ATTESTATION_INVALID,
        ),
    ],
)
def test_a_presence_claim_that_does_not_check_out_is_refused(seeded, label, tamper, expected):
    _p, _q, _pres, outcome = _attempt(seeded, tamper=tamper, label=label)
    assert outcome.status != "completed"
    assert outcome.reason_code is expected


def test_a_presence_attestation_cannot_be_presented_twice(seeded):
    """The second use of the same attestation is refused even against a fresh cart."""
    principals = factory.Principals.create(agent_id="agent:replay", register=True)
    first = _quote(seeded, principals, TEA, "corr_replay_1")
    attestation = factory.issue_presence_attestation(
        principals, checkout_hash=first.checkout_hash
    )

    spec = factory.spec_for_cart(TEA)
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=first.checkout_jwt,
        checkout_hash=first.checkout_hash,
        amount_minor=first.row.total_minor,
        nonce="nonce-replay-1",
    )
    credentials = presentation.credentials
    first_outcome = complete(
        seeded,
        type(credentials)(
            open_checkout=credentials.open_checkout,
            closed_checkout=credentials.closed_checkout,
            open_payment=credentials.open_payment,
            closed_payment=credentials.closed_payment,
            nonce=credentials.nonce,
            presence=attestation,
        ),
        correlation_id="corr_replay_1",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert first_outcome.reason_code is ReasonCode.APPROVED

    second = _quote(seeded, principals, TEA, "corr_replay_2")
    replayed = factory.present(
        principals,
        spec,
        checkout_jwt=second.checkout_jwt,
        checkout_hash=second.checkout_hash,
        amount_minor=second.row.total_minor,
        nonce="nonce-replay-2",
    )
    creds = replayed.credentials
    second_outcome = complete(
        seeded,
        type(creds)(
            open_checkout=creds.open_checkout,
            closed_checkout=creds.closed_checkout,
            open_payment=creds.open_payment,
            closed_payment=creds.closed_payment,
            nonce=creds.nonce,
            presence=attestation,
        ),
        correlation_id="corr_replay_2",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    # The attestation is bound to the first Checkout, so re-using it fails on the binding before
    # replay is even reached. Either refusal is the property being asserted: it is not reusable.
    assert second_outcome.status != "completed"
    assert second_outcome.reason_code in (
        ReasonCode.PRESENCE_REPLAYED,
        ReasonCode.PRESENCE_BINDING_MISMATCH,
    )


def test_presence_does_not_widen_the_cap(seeded):
    """The whole design rests on this. A present person is still bound by what they signed."""
    _p, _q, _pres, outcome = _attempt(
        seeded, cart=HEADPHONES, amount_cap_minor=100_000, label="overcap"
    )
    assert outcome.status != "completed"
    assert outcome.reason_code is ReasonCode.CONSTRAINT_AMOUNT_EXCEEDED


def test_presence_does_not_settle_a_prose_constraint_by_itself(seeded):
    """Being at the keyboard is not the same as having answered the question."""
    _p, _q, _pres, outcome = _attempt(
        seeded, natural_language=["only things we will use this week"], label="prose"
    )
    assert outcome.status != "completed"
    assert outcome.reason_code in (
        ReasonCode.ESCALATION_REQUIRED,
        ReasonCode.ESCALATION_TIMEOUT,
    )


def test_a_signed_confirmation_settles_the_escalation_and_the_purchase(seeded):
    """The path from an unresolved constraint to a completed sale, answered in band.

    An answered escalation is the only route to APPROVED_AFTER_HUMAN_APPROVAL, and nothing reached
    that code until the confirmation flow existed.
    """
    principals = factory.Principals.create(agent_id="agent:confirm", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_confirm")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    issued = factory.issue_open_mandates(principals, spec)

    first = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-confirm-1",
        human_present=True,
    )
    escalated = complete(
        seeded,
        first.credentials,
        correlation_id="corr_confirm",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert escalated.status == "escalated"
    escalation_id = escalated.detail["escalation_id"]

    outcome = escalation_service.record_answer(seeded, escalation_id, "approve")
    assert outcome.accepted

    second = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-confirm-2",
        human_present=True,
    )
    settled = complete(
        seeded,
        second.credentials,
        correlation_id="corr_confirm_2",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert settled.status in ("completed", "awaiting_payment")
    assert settled.reason_code is ReasonCode.APPROVED_AFTER_HUMAN_APPROVAL


def test_an_unanswered_question_still_denies_a_present_human(seeded):
    """Silence fails closed whether or not somebody is sitting there."""
    principals = factory.Principals.create(agent_id="agent:silent", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_silent")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-silent",
        human_present=True,
    )
    escalated = complete(
        seeded,
        presentation.credentials,
        correlation_id="corr_silent",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert escalated.status == "escalated"
    settled = escalation_service.resolve(seeded, escalated.detail["escalation_id"])
    assert settled.status != "approved"


def test_the_evidence_packet_records_that_a_human_was_present(seeded):
    _p, _q, _pres, outcome = _attempt(seeded, label="evidence")
    packets = locker.for_correlation(seeded, "corr_evidence")
    assert packets
    verification = packets[-1].body.get("verification") or {}
    assert verification.get("human_present") is True
    assert (verification.get("presence") or {}).get("digest")
    assert outcome.evidence_packet_id


def test_one_approval_settles_the_checkout_once(seeded):
    """One answered escalation must buy one cart, however many times it is re-presented.

    The escalation rules were never the gap. An agent can mint fresh closed mandates over the same
    merchant-signed Checkout, and they are not replays, so before a Checkout could refuse to be
    decided twice, every re-presentation after an approval settled the cart again.
    """
    principals = factory.Principals.create(agent_id="agent:settle-once", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_once")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    issued = factory.issue_open_mandates(principals, spec)

    def present_again(attempt: int):
        presentation = factory.present_issued(
            issued,
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"nonce-once-{attempt}",
            human_present=True,
        )
        return complete(
            seeded,
            presentation.credentials,
            correlation_id=f"corr_once_{attempt}",
            gateway=StubGateway(),
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
            audience=settings.PUBLIC_BASE_URL,
        )

    escalated = present_again(1)
    assert escalated.status == "escalated"
    escalation_service.record_answer(seeded, escalated.detail["escalation_id"], "approve")

    settled = present_again(2)
    assert settled.reason_code is ReasonCode.APPROVED_AFTER_HUMAN_APPROVAL

    again = present_again(3)
    assert again.status == "refused"
    assert again.reason_code is ReasonCode.CHECKOUT_ALREADY_SETTLED
    assert again.evidence_packet_id, "the refusal is filed like any other"


def test_a_settled_checkout_cannot_be_decided_again(seeded):
    """The same rule without an escalation in the way: an ordinary purchase settles once."""
    principals = factory.Principals.create(agent_id="agent:no-double", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_double")
    issued = factory.issue_open_mandates(principals, factory.spec_for_cart(TEA))

    def present_again(attempt: int):
        presentation = factory.present_issued(
            issued,
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"nonce-double-{attempt}",
        )
        return complete(
            seeded,
            presentation.credentials,
            correlation_id=f"corr_double_{attempt}",
            gateway=StubGateway(),
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
            audience=settings.PUBLIC_BASE_URL,
        )

    first = present_again(1)
    assert first.reason_code is ReasonCode.APPROVED
    second = present_again(2)
    assert second.reason_code is ReasonCode.CHECKOUT_ALREADY_SETTLED


def test_only_the_issuing_surface_can_attest_presence(seeded):
    """Being in the trust registry is not the same as being the screen the person is at.

    A credential provider may be trusted to issue mandates and still have no idea whether anybody
    is sitting in front of anything. Presence has to come from the surface that issued the standing
    authority it is being presented with.
    """
    principals = factory.Principals.create(agent_id="agent:cross-attest", register=True)
    other_surface = factory.Principals.create(
        agent_id="agent:cross-attest-other", issuer_id=factory.SANDBOX_ISSUER, register=True
    )
    quoted = _quote(seeded, principals, TEA, "corr_cross")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(TEA),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-cross",
    )
    borrowed = factory.issue_presence_attestation(
        other_surface, checkout_hash=quoted.checkout_hash
    )
    credentials = presentation.credentials
    result = verify(
        seeded,
        type(credentials)(
            open_checkout=credentials.open_checkout,
            closed_checkout=credentials.closed_checkout,
            open_payment=credentials.open_payment,
            closed_payment=credentials.closed_payment,
            nonce=credentials.nonce,
            presence=borrowed,
        ),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert not result.ok
    assert result.failure.reason_code is ReasonCode.PRESENCE_ISSUER_UNTRUSTED


@pytest.fixture()
def client_and_seed(seeded):
    """A TestClient sharing the seeded session, so a confirmation can be posted at the endpoint."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: seeded
    try:
        with TestClient(app) as client:
            yield client, seeded
    finally:
        app.dependency_overrides.pop(get_db, None)


def _escalate(session):
    """Drive a human-present checkout to an escalation and return what is needed to answer it."""
    principals = factory.Principals.create(agent_id="agent:confirm-bind", register=True)
    quoted = _quote(session, principals, TEA, "corr_confirm_bind")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    issued = factory.issue_open_mandates(principals, spec)
    presentation = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-confirm-bind",
        human_present=True,
    )
    outcome = complete(
        session,
        presentation.credentials,
        correlation_id="corr_confirm_bind",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert outcome.status == "escalated", outcome.reason_code
    session.flush()
    return outcome.detail["escalation_id"], quoted, principals


def test_only_the_issuing_surface_can_answer_an_escalation(client_and_seed):
    """A confirmation is the most powerful thing a surface can sign: it settles money.

    Presence widens nothing, so an attestation from the wrong authority costs little. An approval
    from the wrong authority buys a cart. Both are refused on the same ground.
    """
    client, session = client_and_seed
    escalation_id, quoted, _principals = _escalate(session)

    other = factory.Principals.create(
        agent_id="agent:other-surface", issuer_id=factory.SANDBOX_ISSUER, register=True
    )
    response = client.post(
        "/checkout/confirm",
        json={
            "escalation_id": escalation_id,
            "confirmation": factory.sign_confirmation(
                other,
                escalation_id=escalation_id,
                checkout_hash=quoted.checkout_hash,
                decision="approve",
            ),
        },
    )
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == ReasonCode.PRESENCE_ISSUER_UNTRUSTED.value


def test_an_expired_confirmation_is_refused(client_and_seed):
    """An answer given long enough ago is not an answer to a question asked now."""
    from datetime import timedelta

    from app.db.base import utcnow

    client, session = client_and_seed
    escalation_id, quoted, principals = _escalate(session)

    stale = factory.sign_confirmation(
        principals,
        escalation_id=escalation_id,
        checkout_hash=quoted.checkout_hash,
        decision="approve",
        now=utcnow() - timedelta(hours=2),
    )
    response = client.post(
        "/checkout/confirm", json={"escalation_id": escalation_id, "confirmation": stale}
    )
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == ReasonCode.PRESENCE_ATTESTATION_STALE.value


def test_the_issuing_surface_can_answer(client_and_seed):
    """The control: the same call from the right surface is accepted."""
    client, session = client_and_seed
    escalation_id, quoted, principals = _escalate(session)

    response = client.post(
        "/checkout/confirm",
        json={
            "escalation_id": escalation_id,
            "confirmation": factory.sign_confirmation(
                principals,
                escalation_id=escalation_id,
                checkout_hash=quoted.checkout_hash,
                decision="approve",
            ),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True


def test_the_signed_confirmation_is_kept_with_the_evidence(client_and_seed):
    """An approval that settles money has to be re-checkable by somebody who was not there.

    The endpoint verifies the signature, the issuer, the expiry and both bindings, and then the
    only thing left saying so is the merchant's own assertion that it checked. Keeping the
    confirmation itself means a reviewer with the evidence packet can verify it again.
    """
    from app.db.models import Escalation

    client, session = client_and_seed
    escalation_id, quoted, principals = _escalate(session)
    confirmation = factory.sign_confirmation(
        principals,
        escalation_id=escalation_id,
        checkout_hash=quoted.checkout_hash,
        decision="approve",
    )

    response = client.post(
        "/checkout/confirm",
        json={"escalation_id": escalation_id, "confirmation": confirmation},
    )
    assert response.status_code == 200, response.text

    evidence = escalation_service.as_evidence(session, session.get(Escalation, escalation_id))
    accepted = [r for r in evidence["responses"] if r["accepted"]]
    assert [r["proof"] for r in accepted] == [confirmation]


def test_a_present_human_is_not_also_messaged(seeded):
    """Presence changes who is asked, not what is decided.

    The person is at the screen and the escalation comes back in the response, so a WhatsApp
    message would be a second question nobody needs. It also matters that none was sent: an
    escalation nobody was messaged about must not then accept an inbound WhatsApp answer.
    """
    from app.db.models import Escalation

    transport = RecordingTransport()
    principals = factory.Principals.create(agent_id="agent:present-quiet", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_present_quiet")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    issued = factory.issue_open_mandates(principals, spec)
    presentation = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-present-quiet",
        human_present=True,
    )
    outcome = complete(
        seeded,
        presentation.credentials,
        correlation_id="corr_present_quiet",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=transport,
        audience=settings.PUBLIC_BASE_URL,
    )

    assert outcome.status == "escalated"
    assert transport.sent == []
    escalation = seeded.get(Escalation, outcome.detail["escalation_id"])
    assert escalation.sent_to is None

    refused = escalation_service.record_answer(
        seeded, escalation.id, "approve", from_number=settings.ESCALATION_HUMAN_WHATSAPP or "+919876543210"
    )
    assert not refused.accepted
    assert refused.ignored_reason == "sender_is_not_the_principal"


def test_an_absent_human_is_still_messaged(seeded):
    """The control, so the branch above cannot silently swallow every escalation."""
    from app.db.models import Escalation

    transport = RecordingTransport()
    principals = factory.Principals.create(agent_id="agent:absent-loud", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_absent_loud")
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    issued = factory.issue_open_mandates(principals, spec)
    presentation = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-absent-loud",
    )
    outcome = complete(
        seeded,
        presentation.credentials,
        correlation_id="corr_absent_loud",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=transport,
        audience=settings.PUBLIC_BASE_URL,
    )

    assert outcome.status == "escalated"
    escalation = seeded.get(Escalation, outcome.detail["escalation_id"])
    if settings.ESCALATION_HUMAN_WHATSAPP:
        assert transport.sent
        assert escalation.sent_to
    else:
        assert escalation.delivery_error


def test_a_second_presentation_cannot_choose_who_answers(client_and_seed):
    """An escalated Checkout is still presentable, and every presentation rewrites its mandate.

    So the question of who may answer cannot be read off the Checkout at confirm time. It is
    pinned to the surface whose authority was being spent when the question was put; otherwise an
    agent holding a second mandate from a different registered surface could re-present, repoint
    the Checkout at it, and have that surface answer a question put to the first one.
    """
    from app.db.models import CheckoutSession

    client, session = client_and_seed
    escalation_id, quoted, _principals = _escalate(session)
    first_mandate = session.get(CheckoutSession, quoted.row.id).mandate_id

    other_surface = factory.Principals.create(
        agent_id="agent:repoint", issuer_id=factory.SANDBOX_ISSUER, register=True
    )
    second = factory.present(
        other_surface,
        factory.spec_for_cart(TEA, natural_language=["only things we will use this week"]),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-repoint",
    )
    complete(
        session,
        second.credentials,
        correlation_id="corr_repoint",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    session.flush()
    session.expire(quoted.row)
    assert session.get(CheckoutSession, quoted.row.id).mandate_id != first_mandate

    response = client.post(
        "/checkout/confirm",
        json={
            "escalation_id": escalation_id,
            "confirmation": factory.sign_confirmation(
                other_surface,
                escalation_id=escalation_id,
                checkout_hash=quoted.checkout_hash,
                decision="approve",
            ),
        },
    )

    assert response.status_code >= 400
    assert response.json()["error"]["code"] == ReasonCode.PRESENCE_ISSUER_UNTRUSTED.value


def test_an_unreadable_confirmation_is_refused_with_a_reason_code(client_and_seed):
    """Unauthenticated input decides nothing, including how the process reports its own failure.

    A segment that base64-decodes to bytes which are not UTF-8 raises UnicodeDecodeError rather
    than a JSON error, which is not what the decoder used to catch. A 500 carries no reason code
    and files no evidence, so it is a hole in fail-closed rather than a cosmetic difference.
    """
    import base64

    client, session = client_and_seed
    escalation_id, _quoted, _principals = _escalate(session)
    segment = base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode().rstrip("=")

    response = client.post(
        "/checkout/confirm",
        json={"escalation_id": escalation_id, "confirmation": f"{segment}.{segment}.sig"},
    )

    assert 400 <= response.status_code < 500, response.status_code
    assert response.json()["error"]["code"] == ReasonCode.PRESENCE_ATTESTATION_INVALID.value


def test_the_cart_outlives_the_question_put_to_the_human(seeded):
    """The deadline the human is given is longer than the quote it is about.

    An escalation runs for ESCALATION_DEADLINE_SECONDS, the quote and its inventory hold for
    QUOTE_TTL_SECONDS, and the first is deliberately the longer of the two. If the Checkout were
    left on its original expiry, an approval given well inside the window we advertised would come
    back to a quote that had already lapsed, and the purchase would be refused as CHECKOUT_EXPIRED
    with nothing saying the real cause was our own two clocks disagreeing.
    """
    from app.db.models import Escalation, HoldStatus, InventoryHold

    principals = factory.Principals.create(agent_id="agent:deadline", register=True)
    quoted = _quote(seeded, principals, TEA, "corr_deadline")
    original_expiry = quoted.row.expires_at
    spec = factory.spec_for_cart(TEA, natural_language=["only things we will use this week"])
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce="nonce-deadline",
    )
    outcome = complete(
        seeded,
        presentation.credentials,
        correlation_id="corr_deadline",
        gateway=StubGateway(),
        semantic_client=KeywordSemanticClient(),
        whatsapp=RecordingTransport(),
        audience=settings.PUBLIC_BASE_URL,
    )
    assert outcome.status == "escalated"

    escalation = seeded.get(Escalation, outcome.detail["escalation_id"])
    seeded.refresh(quoted.row)
    assert escalation.deadline_at > original_expiry, "the premise: the deadline outlives the quote"
    assert quoted.row.expires_at >= escalation.deadline_at

    holds = seeded.query(InventoryHold).filter_by(
        checkout_id=quoted.row.id, status=HoldStatus.HELD
    ).all()
    assert holds
    for hold in holds:
        assert hold.expires_at >= escalation.deadline_at
