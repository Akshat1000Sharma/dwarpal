"""The template probe, against a recording transport.

The probe exists to make a real send, but the suite must never need Meta credentials, so every
test here drives it through RecordingTransport and asserts on the payload it would have sent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.escalation.whatsapp import APPROVE_ID, DENY_ID, RecordingTransport
from app.settings import settings
from app.template_probe import send_template_probe


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    defaults = {
        "META_TEMPLATE_NAME": "dwarpal_purchase_approval",
        "META_TEMPLATE_LANGUAGE": "en",
        "META_RECEIPT_TEMPLATE_NAME": "dwarpal_purchase_receipt",
        "META_RECEIPT_TEMPLATE_LANGUAGE": "en",
        "ESCALATION_HUMAN_WHATSAPP": "+919876543210",
        "MERCHANT_NAME": "Dwarpal Demo Store",
    }
    for key, value in {**defaults, **overrides}.items():
        monkeypatch.setattr(settings, key, value)


class _Recorder(RecordingTransport):
    """RecordingTransport, but answering with the message id shape Meta actually returns."""

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        super().send(payload)
        return {"messages": [{"id": f"wamid.TEST{len(self.sent)}"}]}


def test_both_templates_are_sent_and_their_ids_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    transport = _Recorder()

    results = send_template_probe(transport)

    assert [r.label for r in results] == ["escalation", "receipt"]
    assert all(r.ok for r in results)
    assert [r.message_id for r in results] == ["wamid.TEST1", "wamid.TEST2"]
    assert [p["type"] for p in transport.sent] == ["template", "template"]
    assert transport.sent[0]["template"]["name"] == "dwarpal_purchase_approval"
    assert transport.sent[1]["template"]["name"] == "dwarpal_purchase_receipt"


def test_every_template_send_carries_five_body_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meta rejects the whole message on a parameter count the template was not approved with."""
    _configure(monkeypatch)
    transport = _Recorder()

    send_template_probe(transport)

    for payload in transport.sent:
        body = [c for c in payload["template"]["components"] if c["type"] == "body"]
        assert len(body) == 1
        assert len(body[0]["parameters"]) == 5
        assert all(p["text"] for p in body[0]["parameters"])


def test_the_approval_probe_keeps_approve_at_index_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index order is the whole safety property: index 0 must carry approve, never deny."""
    _configure(monkeypatch)
    transport = _Recorder()

    send_template_probe(transport)

    buttons = [c for c in transport.sent[0]["template"]["components"] if c["type"] == "button"]
    assert [b["index"] for b in buttons] == ["0", "1"]
    assert buttons[0]["parameters"][0]["payload"].startswith(f"{APPROVE_ID}:")
    assert buttons[1]["parameters"][0]["payload"].startswith(f"{DENY_ID}:")


def test_the_probe_never_reads_as_a_real_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    """It reaches a real phone, so the parameters that carry meaning must say what it is."""
    _configure(monkeypatch)
    transport = _Recorder()

    send_template_probe(transport)

    receipt_body = transport.sent[1]["template"]["components"][0]["parameters"]
    assert "no purchase was made" in receipt_body[3]["text"].lower()
    assert "test" in receipt_body[2]["text"].lower()


def test_the_recipient_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    transport = _Recorder()

    send_template_probe(transport, to_number="+919000000001")

    assert {p["to"] for p in transport.sent} == {"+919000000001"}


def test_no_recipient_is_a_reported_failure_not_a_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, ESCALATION_HUMAN_WHATSAPP="")
    transport = _Recorder()

    results = send_template_probe(transport)

    assert not any(r.ok for r in results)
    assert transport.sent == []
    assert all("ESCALATION_HUMAN_WHATSAPP" in r.error for r in results)


def test_an_unconfigured_template_is_reported_and_the_other_still_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, META_TEMPLATE_NAME="")
    transport = _Recorder()

    results = send_template_probe(transport)

    assert not results[0].ok and "no template name configured" in results[0].error
    assert results[1].ok
    assert len(transport.sent) == 1


def test_a_refused_send_is_a_result_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One template failing while the other succeeds is exactly the case worth seeing."""
    _configure(monkeypatch)

    class _Failing(_Recorder):
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload["template"]["name"] == "dwarpal_purchase_approval":
                request = httpx.Request("POST", "https://graph.facebook.com/v23.0/x/messages")
                response = httpx.Response(
                    400,
                    json={"error": {"message": "Template name does not exist", "code": 132001}},
                    request=request,
                )
                raise httpx.HTTPStatusError("400", request=request, response=response)
            return super().send(payload)

    results = send_template_probe(_Failing())

    assert not results[0].ok
    assert "Template name does not exist" in results[0].error
    assert "132001" in results[0].error
    assert results[1].ok


def test_a_response_without_a_message_id_is_not_counted_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with no id is not proof of anything, so it must not read as a success."""
    _configure(monkeypatch)

    class _Empty(RecordingTransport):
        def send(self, payload: dict[str, Any]) -> dict[str, Any]:
            super().send(payload)
            return {}

    results = send_template_probe(_Empty())

    assert not any(r.ok for r in results)
    assert all("no message id" in r.error for r in results)
