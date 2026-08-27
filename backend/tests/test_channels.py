"""The outbound channel preflight.

Written after a live run found that META_TEMPLATE_NAME pointed at a template nobody had ever
created. Meta does not complain about that until the first escalation needs it, at which point the
send fails and the free-form fallback quietly covers for it, so the misconfiguration can sit there
for weeks. These tests pin the shapes that check has to catch.

Nothing here contacts Meta. The one function that makes a request is replaced.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app import channels
from app.db.models import utcnow
from app.settings import settings


def _stub_graph(monkeypatch, *, templates: list[dict[str, str]] | None = None,
                webhook: str | None = None, token_valid: bool = True) -> None:
    """Answer every Graph read from a fixture instead of the network."""

    def fake_get(url: str, token: str, params: dict[str, Any] | None = None):
        del token
        if "debug_token" in url:
            return 200, {
                "data": {
                    "is_valid": token_valid,
                    "type": "SYSTEM_USER",
                    "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"],
                }
            }
        if "message_templates" in url:
            return 200, {"data": templates or []}
        return 200, {
            "display_phone_number": "+91 99999 00001",
            "verified_name": "Dwarpal Demo",
            "quality_rating": "GREEN",
            "webhook_configuration": {
                "application": webhook
                if webhook is not None
                else settings.PUBLIC_BASE_URL.rstrip("/") + "/webhooks/whatsapp"
            },
        }

    monkeypatch.setattr(channels, "_get", fake_get)


def _named(report: channels.Report, needle: str) -> channels.Check:
    matches = [c for c in report.checks if needle in c.name]
    assert matches, f"no check named like {needle!r} in {[c.name for c in report.checks]}"
    return matches[0]


def _configure(monkeypatch, **overrides: str) -> None:
    defaults = {
        "META_ACCESS_TOKEN": "test-token",
        "META_PHONE_NUMBER_ID": "100000000000001",
        "META_WABA_ID": "200000000000002",
        "ESCALATION_HUMAN_WHATSAPP": "+919876543210",
        "META_TEMPLATE_NAME": "",
        "META_TEMPLATE_LANGUAGE": "en",
        "META_RECEIPT_TEMPLATE_NAME": "",
        "META_RECEIPT_TEMPLATE_LANGUAGE": "en",
    }
    for key, value in {**defaults, **overrides}.items():
        monkeypatch.setattr(settings, key, value)


def test_a_template_that_does_not_exist_is_caught(monkeypatch) -> None:
    """The defect this whole module exists for."""
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval")
    _stub_graph(monkeypatch, templates=[{"name": "hello_world", "language": "en_US",
                                         "status": "APPROVED"}])

    report = channels.run()
    check = _named(report, "template META_TEMPLATE_NAME")

    assert not check.ok
    assert "does not exist at all" in check.detail
    assert "free-form" in check.fix, "the fix must say what happens if it is left unset"


def test_a_template_in_the_wrong_language_names_the_right_one(monkeypatch) -> None:
    """en against a template published as en_US is the easiest way to get this wrong."""
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval",
               META_TEMPLATE_LANGUAGE="en")
    _stub_graph(monkeypatch, templates=[
        {"name": "dwarpal_purchase_approval", "language": "en_US", "status": "APPROVED"},
    ])

    check = _named(channels.run(), "template META_TEMPLATE_NAME")

    assert not check.ok
    assert "does not exist in en" in check.detail
    assert "en_US" in check.detail, "it must say which language it does exist in"


def test_a_template_awaiting_approval_is_not_treated_as_usable(monkeypatch) -> None:
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval")
    _stub_graph(monkeypatch, templates=[
        {"name": "dwarpal_purchase_approval", "language": "en", "status": "PENDING"},
    ])

    check = _named(channels.run(), "template META_TEMPLATE_NAME")

    assert not check.ok
    assert "PENDING" in check.detail


def test_an_approved_template_passes(monkeypatch) -> None:
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval")
    _stub_graph(monkeypatch, templates=[
        {"name": "dwarpal_purchase_approval", "language": "en", "status": "APPROVED"},
    ])

    assert _named(channels.run(), "template META_TEMPLATE_NAME").ok


def test_no_template_configured_is_fine_but_says_what_that_costs(monkeypatch) -> None:
    _configure(monkeypatch)
    _stub_graph(monkeypatch)

    check = _named(channels.run(), "message templates")

    assert check.ok
    assert "24 hour" in check.fix, "the operator must be told free-form has a window"


def test_a_configured_template_with_no_waba_id_fails_rather_than_skipping(monkeypatch) -> None:
    """A check that quietly skips the thing it was written for is worse than no check."""
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval", META_WABA_ID="")
    _stub_graph(monkeypatch)

    check = _named(channels.run(), "message templates")

    assert not check.ok
    assert "META_WABA_ID" in check.detail


def test_a_webhook_pointing_somewhere_else_is_caught(monkeypatch) -> None:
    """Replies going to a stale tunnel is silent until somebody taps a button."""
    _configure(monkeypatch)
    _stub_graph(monkeypatch, webhook="https://an-old-tunnel.example/webhooks/whatsapp")

    check = _named(channels.run(), "webhook points back here")

    assert not check.ok
    assert "an-old-tunnel.example" in check.detail


@pytest.mark.parametrize("number", ["", "not a number", "919000000001", "+0123"])
def test_a_recipient_that_would_never_deliver_is_caught(monkeypatch, number: str) -> None:
    _configure(monkeypatch, ESCALATION_HUMAN_WHATSAPP=number)
    _stub_graph(monkeypatch)

    assert not _named(channels.run(), "recipient is E.164").ok


def test_missing_credentials_stop_before_calling_out(monkeypatch) -> None:
    """With no token there is nothing to ask, and the report should say so plainly."""
    _configure(monkeypatch, META_ACCESS_TOKEN="")

    def explode(*_args, **_kwargs):
        raise AssertionError("the preflight must not call Graph without credentials")

    monkeypatch.setattr(channels, "_get", explode)

    report = channels.run()
    check = _named(report, "whatsapp credentials present")
    assert not check.ok


def test_the_report_summarises_itself(monkeypatch) -> None:
    _configure(monkeypatch, META_TEMPLATE_NAME="nope")
    _stub_graph(monkeypatch, templates=[])

    document = channels.run().as_dict()

    assert document["ok"] is False
    assert document["failed"] >= 1
    assert document["checked"] == len(document["checks"])
    assert all({"name", "ok", "detail", "fix"} <= set(c) for c in document["checks"])


def test_the_preflight_never_sends_anything(monkeypatch) -> None:
    """It is a preflight. If it could send, nobody would run it against production."""
    import app.escalation.whatsapp as whatsapp

    def explode(*_args, **_kwargs):
        raise AssertionError("the preflight sent a message")

    monkeypatch.setattr(whatsapp.CloudApiTransport, "send", explode)
    _configure(monkeypatch, META_TEMPLATE_NAME="dwarpal_purchase_approval")
    _stub_graph(monkeypatch, templates=[
        {"name": "dwarpal_purchase_approval", "language": "en", "status": "APPROVED"},
    ])

    channels.run()


# --------------------------------------------------------------- shared account isolation


def _button_payload(phone_number_id: str, escalation_id: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "200000000000002", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "919999900001",
                         "phone_number_id": phone_number_id},
            "messages": [{
                "from": "919000000001", "id": "wamid.test", "type": "interactive",
                "interactive": {"type": "button_reply",
                                "button_reply": {"id": f"dwarpal_approve:{escalation_id}",
                                                 "title": "Approve"}},
            }],
        }}]}],
    }


def test_a_reply_on_another_number_of_the_same_account_is_ignored() -> None:
    """A WhatsApp Business Account can hold several numbers, and a subscription is per account.

    Every app subscribed to the account receives every number's events. Found live: three apps
    were subscribed to one account, so a button tapped on this merchant's number was also
    delivered to two other products, and one of them replied from a different number. The mirror
    of that is what this guards: somebody else's reply must never settle an escalation here.
    """
    from app.escalation import whatsapp

    mine, theirs = "100000000000001", "100000000000009"

    assert whatsapp.parse_inbound(_button_payload(mine, "esc-1"), phone_number_id=mine)
    assert whatsapp.parse_inbound(_button_payload(theirs, "esc-1"), phone_number_id=mine) == []


def test_the_numbers_a_payload_touches_are_reported() -> None:
    from app.escalation import whatsapp

    payload = _button_payload("100000000000009", "esc-1")
    assert whatsapp.numbers_in(payload) == {"100000000000009"}


def test_a_payload_with_no_metadata_is_still_read() -> None:
    """Meta always sends metadata, but a payload without it must not be silently dropped."""
    from app.escalation import whatsapp

    payload = {
        "entry": [{"changes": [{"value": {"messages": [{
            "from": "919000000001", "id": "wamid.x", "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "dwarpal_deny:esc-9", "title": "Deny"}},
        }]}}]}],
    }
    answers = whatsapp.parse_inbound(payload, phone_number_id="100000000000001")
    assert len(answers) == 1 and answers[0].answer == "deny"


# --------------------------------------------------------------- who is allowed to answer


def _pending(db: Session, sent_to: str | None) -> str:
    """An escalation waiting for an answer, addressed to a particular handset."""
    from app.db.models import Escalation

    row = Escalation(
        id="esc-sender-check",
        correlation_id="corr-sender-check",
        checkout_id="chk-sender-check",
        agent_id="agent-under-test",
        constraint_text="only if it is the good stuff",
        raised_reason="SEMANTIC_ESCALATION",
        amount_minor=250000,
        cart_fingerprint="fp-sender-check",
        deadline_at=utcnow() + timedelta(minutes=30),
        sent_to=sent_to,
    )
    db.add(row)
    db.flush()
    return row.id


def test_only_the_number_that_was_asked_can_answer(db: Session, monkeypatch) -> None:
    """The escalation id travels back to the agent in its own 202 response.

    A plain "approve <id>" text is a valid answer, so if the sender were not checked, the agent
    that triggered the escalation could approve itself from any handset it controls. Nothing else
    on this path authenticates the answer: Meta's webhook signature proves the message came from
    Meta, not that it came from the person who was asked.
    """
    from app.escalation import service as escalation_service

    monkeypatch.setattr(settings, "ESCALATION_HUMAN_WHATSAPP", "+919876543210")
    escalation_id = _pending(db, sent_to="+919876543210")

    outcome = escalation_service.record_answer(
        db, escalation_id, "approve", from_number="+919000000001"
    )

    assert not outcome.accepted
    assert outcome.ignored_reason == "sender_is_not_the_principal"
    assert outcome.status == "pending"


def test_the_number_that_was_asked_is_obeyed(db: Session, monkeypatch) -> None:
    from app.escalation import service as escalation_service

    monkeypatch.setattr(settings, "ESCALATION_HUMAN_WHATSAPP", "+919876543210")
    escalation_id = _pending(db, sent_to="+919876543210")

    outcome = escalation_service.record_answer(
        db, escalation_id, "approve", from_number="+91 98765 43210"
    )

    assert outcome.accepted
    assert outcome.status == "approved"


def test_an_answer_to_an_escalation_that_reached_nobody_is_not_applied(
    db: Session, monkeypatch
) -> None:
    """Delivery can fail, and then there is no principal to have answered."""
    from app.escalation import service as escalation_service

    monkeypatch.setattr(settings, "ESCALATION_HUMAN_WHATSAPP", "")
    escalation_id = _pending(db, sent_to=None)

    outcome = escalation_service.record_answer(
        db, escalation_id, "approve", from_number="+919000000001"
    )

    assert not outcome.accepted
    assert outcome.ignored_reason == "sender_is_not_the_principal"


def test_a_sender_that_is_not_a_number_at_all_is_refused(db: Session, monkeypatch) -> None:
    from app.escalation import service as escalation_service

    monkeypatch.setattr(settings, "ESCALATION_HUMAN_WHATSAPP", "+919876543210")
    escalation_id = _pending(db, sent_to="+919876543210")

    outcome = escalation_service.record_answer(db, escalation_id, "approve", from_number="not-a-number")

    assert not outcome.accepted
    assert outcome.ignored_reason == "sender_is_not_the_principal"


def test_the_refused_attempt_is_still_recorded(db: Session, monkeypatch) -> None:
    """An impersonation attempt is evidence, so it is written down rather than dropped."""
    from app.db.models import EscalationResponse
    from app.escalation import service as escalation_service

    monkeypatch.setattr(settings, "ESCALATION_HUMAN_WHATSAPP", "+919876543210")
    escalation_id = _pending(db, sent_to="+919876543210")
    escalation_service.record_answer(db, escalation_id, "approve", from_number="+919000000001")

    rows = db.query(EscalationResponse).filter_by(escalation_id=escalation_id).all()
    assert [r.ignored_reason for r in rows] == ["sender_is_not_the_principal"]
