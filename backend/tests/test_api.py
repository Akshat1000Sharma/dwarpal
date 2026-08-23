"""The HTTP surface: agent endpoints, webhook signatures, and the machine-actionable errors."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.harness import factory
from app.kernel.reasons import ACTIONS, ReasonCode
from app.settings import settings


@pytest.fixture()
def client(seeded, gateway) -> Iterator[TestClient]:
    """A live app wired to the test session and the stub gateway."""
    from app.api import deps
    from app.main import create_app
    from app.payments import gateway as gateway_module

    gateway_module.set_gateway(gateway)
    application = create_app()
    application.dependency_overrides[deps.get_db] = lambda: seeded

    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client

    application.dependency_overrides.clear()
    gateway_module.set_gateway(None)


# --- discovery and catalog -------------------------------------------------------------------


def test_discovery_document_tells_an_agent_everything_it_needs(client):
    body = client.get("/.well-known/ap2-merchant").json()

    assert body["protocols"]["ap2"]["flows"] == ["human-not-present"]
    assert body["roles_implemented"] == ["merchant", "merchant_payment_processor"]
    assert body["roles_mocked"] == ["credential_provider"]
    assert {c["vct"] for c in body["accepted_credentials"]} == {
        "mandate.checkout.open.1",
        "mandate.checkout.1",
        "mandate.payment.open.1",
        "mandate.payment.1",
    }
    for key in ("catalog_browse", "quote", "checkout_complete", "policy_terms"):
        assert body["endpoints"][key].startswith("http")
    assert body["policy"]["current_hash"]
    assert body["trust_anchors"]
    assert body["unverified_access"]["challenge_status"] == 402


def test_jwks_is_served_and_usable(client):
    body = client.get("/.well-known/jwks.json").json()
    key = body["keys"][0]
    assert key["kty"] == "EC" and key["crv"] == "P-256" and key["alg"] == "ES256"

    from app.ap2.jose import public_key_from_jwk

    assert public_key_from_jwk(key) is not None


def test_catalog_carries_machine_readable_constraints_and_live_counts(client):
    body = client.get("/catalog/items").json()
    assert body["count"] >= 12
    item = next(i for i in body["items"] if i["sku"] == "DWP-WIN-005")
    constraints = item["purchase_constraints"]
    assert constraints["age_restricted"] is True
    assert constraints["restricted_category"] is True
    assert constraints["region_locked"]
    assert constraints["min_order_quantity"] >= 1
    assert "available_quantity" in item["availability"]


def test_catalog_search_and_categories(client):
    found = client.get("/catalog/search", params={"q": "lamp"}).json()
    assert {i["sku"] for i in found["items"]} == {"DWP-LMP-009", "DWP-LMP-010"}
    assert "alcohol" in client.get("/catalog/categories").json()["categories"]


def test_unknown_item_returns_a_machine_actionable_error(client):
    response = client.get("/catalog/items/DWP-NOPE-000")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == ReasonCode.ITEM_UNKNOWN.value
    assert error["action"] == ACTIONS[ReasonCode.ITEM_UNKNOWN].value
    assert "retryable" in error
    assert error["correlation_id"]


def test_policy_terms_are_signed_and_addressed_by_hash(client):
    body = client.get("/policy/terms").json()
    assert body["content_hash"]
    assert body["signed_jwt"].count(".") == 2
    again = client.get(f"/policy/terms/{body['content_hash']}").json()
    assert again["body"] == body["body"]


# --- the purchase path over HTTP ----------------------------------------------------------------


def test_quote_then_complete_over_http(client, seeded):
    quote_response = client.post(
        "/checkout/quote",
        json={"items": [{"sku": "DWP-TEA-001", "quantity": 2}]},
        headers={"X-Agent-Id": "agent:http"},
    )
    assert quote_response.status_code == 200
    quoted = quote_response.json()
    assert quoted["checkout_hash"]
    assert quoted["next"]["closed_payment_mandate"]["transaction_id"] == quoted["checkout_hash"]

    principals = factory.Principals.create(agent_id="agent:http")
    presentation = factory.present(
        principals,
        factory.spec_for_cart([("DWP-TEA-001", "Nilgiri Black Tea 250g", 2)]),
        checkout_jwt=quoted["checkout_jwt"],
        checkout_hash=quoted["checkout_hash"],
        amount_minor=quoted["total"]["amount"],
        audience=settings.PUBLIC_BASE_URL,
    )
    credentials = presentation.credentials

    completion = client.post(
        "/checkout/complete",
        json={
            "open_checkout_mandate": credentials.open_checkout,
            "closed_checkout_mandate": credentials.closed_checkout,
            "open_payment_mandate": credentials.open_payment,
            "closed_payment_mandate": credentials.closed_payment,
            "nonce": credentials.nonce,
        },
    )
    assert completion.status_code == 200, completion.text
    body = completion.json()
    assert body["status"] == "completed"
    assert body["receipt"]["status"] == "Success"
    assert body["evidence_packet_id"]

    from app.ap2.schema_validation import assert_conforms

    assert_conforms("checkout_receipt", body["receipt"])


def test_a_policy_refusal_tells_the_agent_what_to_do_next(client, seeded):
    """A refusal returned rather than raised must still be machine-actionable.

    Every AgentError carries an action and a retryable flag. A kernel refusal takes a different
    route out of the application, and this is the route where money is at stake, so it is the last
    one that should force an agent to parse prose.
    """
    from app.db.models import AgentIdentity

    agent = "agent:refusal-envelope"
    cart = [("DWP-HDP-007", "Wireless Headphones", 1)]
    principals = factory.Principals.create(agent_id=agent)

    def attempt(nonce: str):
        quoted = client.post(
            "/checkout/quote",
            json={"items": [{"sku": "DWP-HDP-007", "quantity": 1}]},
            headers={"X-Agent-Id": agent},
        ).json()
        credentials = factory.present(
            principals,
            factory.spec_for_cart(cart),
            checkout_jwt=quoted["checkout_jwt"],
            checkout_hash=quoted["checkout_hash"],
            amount_minor=quoted["total"]["amount"],
            audience=settings.PUBLIC_BASE_URL,
            nonce=nonce,
        ).credentials
        return client.post(
            "/checkout/complete",
            json={
                "open_checkout_mandate": credentials.open_checkout,
                "closed_checkout_mandate": credentials.closed_checkout,
                "open_payment_mandate": credentials.open_payment,
                "closed_payment_mandate": credentials.closed_payment,
                "nonce": credentials.nonce,
            },
            headers={"X-Agent-Id": agent},
        )

    # The agent identity the gate attaches to is created by its first completion.
    allowed = attempt("refusal-envelope-1")
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["action"] == ACTIONS[ReasonCode.APPROVED].value

    seeded.execute(
        update(AgentIdentity)
        .where(AgentIdentity.agent_id == agent)
        .values(blocked_categories=["electronics"])
    )
    seeded.flush()

    refusal = attempt("refusal-envelope-2")
    assert refusal.status_code == 403, refusal.text
    body = refusal.json()
    assert body["reason_code"] == ReasonCode.CATEGORY_FORBIDDEN.value
    assert body["action"] == ACTIONS[ReasonCode.CATEGORY_FORBIDDEN].value
    assert body["action"] == "reduce_cart"
    assert body["retryable"] is False


def test_quote_for_an_unknown_sku_is_machine_actionable(client):
    response = client.post("/checkout/quote", json={"items": [{"sku": "NOPE", "quantity": 1}]})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ReasonCode.ITEM_UNKNOWN.value


def test_quote_beyond_the_item_maximum_is_refused(client):
    response = client.post(
        "/checkout/quote", json={"items": [{"sku": "DWP-MNG-004", "quantity": 99}]}
    )
    assert response.status_code in (403, 409)
    error = response.json()["error"]
    assert error["code"] == ReasonCode.QUANTITY_OUT_OF_RANGE.value
    assert error["action"] == "reduce_cart"


def test_an_idempotent_quote_replays_rather_than_re_holding(client):
    payload = {"items": [{"sku": "DWP-NTB-011", "quantity": 1}]}
    headers = {"Idempotency-Key": "quote-key-1", "X-Agent-Id": "agent:idem"}
    first = client.post("/checkout/quote", json=payload, headers=headers).json()
    second = client.post("/checkout/quote", json=payload, headers=headers).json()
    assert first["checkout_id"] == second["checkout_id"]


def test_correlation_id_is_echoed(client):
    response = client.get("/health", headers={"X-Correlation-Id": "dwc_supplied"})
    assert response.headers["X-Correlation-Id"] == "dwc_supplied"


# --- webhooks -----------------------------------------------------------------------------------


def _razorpay_signature(body: bytes) -> str:
    return hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


def _meta_signature(body: bytes) -> str:
    return "sha256=" + hmac.new(
        settings.META_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


RAZORPAY_EVENT = {
    "event": "payment.captured",
    "payload": {"payment": {"entity": {"id": "pay_x", "order_id": "order_x", "amount": 100}}},
}


def test_razorpay_webhook_rejects_an_unsigned_notification(client):
    response = client.post("/webhooks/razorpay", json=RAZORPAY_EVENT)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == ReasonCode.WEBHOOK_SIGNATURE_INVALID.value


def test_razorpay_webhook_rejects_a_mis_signed_notification(client):
    body = json.dumps(RAZORPAY_EVENT).encode()
    response = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": "00" * 32}
    )
    assert response.status_code == 401


def test_razorpay_webhook_accepts_a_correctly_signed_notification(client):
    body = json.dumps(RAZORPAY_EVENT).encode()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _razorpay_signature(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["event"] == "payment.captured"


def test_razorpay_signature_covers_the_exact_bytes(client):
    body = json.dumps(RAZORPAY_EVENT).encode()
    signature = _razorpay_signature(body)
    tampered = body.replace(b"pay_x", b"pay_y")
    response = client.post(
        "/webhooks/razorpay", content=tampered, headers={"X-Razorpay-Signature": signature}
    )
    assert response.status_code == 401


def test_a_discrepancy_can_be_reconciled_without_rewriting_either_record(client, seeded):
    """Resolving says a human dealt with it. It must not edit what either side reported."""
    from app.db.models import PaymentException

    filed = PaymentException(
        correlation_id="dwc_resolvable",
        payment_id="pay_resolvable",
        kind="gateway_holds_money_for_a_checkout_we_will_not_settle",
        local_state={"checkout_state": "cancelled"},
        gateway_state={"id": "pay_resolvable", "status": "authorized", "amount": 45000},
    )
    seeded.add(filed)
    seeded.flush()

    listed = client.get("/merchant/exceptions").json()["exceptions"]
    assert any(e["id"] == filed.id and e["resolved"] is False for e in listed)

    response = client.post(f"/merchant/exceptions/{filed.id}/resolve")
    assert response.status_code == 200, response.text
    assert response.json()["resolved"] is True

    assert filed.resolved is True
    assert filed.local_state == {"checkout_state": "cancelled"}
    assert filed.gateway_state["status"] == "authorized"

    assert client.post("/merchant/exceptions/does-not-exist/resolve").status_code == 404


def test_money_held_for_a_cancelled_checkout_is_filed_as_a_discrepancy(client, seeded, gateway):
    """The gateway can authorise money for a checkout Dwarpal has already closed.

    Refusing to settle it is correct. Refusing silently is not: the buyer's money is held against
    an order that will never be fulfilled and nothing reconciles it back.
    """
    from app.checkout.complete import finalise_failed
    from app.db.models import (
        CheckoutSession,
        CheckoutState,
        Payment,
        PaymentException,
        PaymentStatus,
    )
    from tests.test_money_paths import _awaiting

    correlation = "dwc_orphaned_money"
    outcome = _awaiting(seeded, gateway, correlation)
    payment = seeded.get(Payment, outcome.payment_id)
    payment.razorpay_payment_id = "pay_orphaned"
    payment.status = PaymentStatus.FAILED
    seeded.flush()
    finalise_failed(seeded, payment)
    assert seeded.get(CheckoutSession, outcome.checkout_id).state == CheckoutState.CANCELLED

    def deliver() -> dict[str, Any]:
        body = json.dumps(
            {
                "entity": "event",
                "event": "payment.authorized",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_orphaned",
                            "entity": "payment",
                            "amount": payment.amount_minor,
                            "currency": "INR",
                            "status": "authorized",
                            "method": "netbanking",
                        }
                    }
                },
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
        )
        assert response.status_code == 200, response.text
        return response.json()

    assert "reconciliation.exception" in deliver()["handled"]
    # A redelivered webhook must not file the same disagreement twice.
    assert "reconciliation.exception" not in deliver()["handled"]

    filed = seeded.scalars(
        select(PaymentException).where(PaymentException.correlation_id == correlation)
    ).all()
    assert len(filed) == 1
    assert filed[0].local_state["checkout_state"] == CheckoutState.CANCELLED
    assert filed[0].gateway_state["status"] == "authorized"
    assert filed[0].resolved is False

    # Nothing may be settled on the strength of that authorisation.
    assert seeded.get(CheckoutSession, outcome.checkout_id).state == CheckoutState.CANCELLED


def test_a_refund_that_fails_after_creation_is_filed_as_a_discrepancy(client, seeded):
    """A compensating refund can succeed at creation and fail later, asynchronously.

    The checkout then claims it was compensated while the buyer never got the money back. Razorpay
    is authoritative, so the disagreement is recorded rather than silently corrected.
    """
    from app.db.models import PaymentException, Refund, RefundStatus

    refund = Refund(
        payment_id="pay_local_ref",
        correlation_id="dwc_refund_failed",
        razorpay_refund_id="rfnd_failed_later",
        amount_minor=45000,
        reason="revocation_after_capture",
        status=RefundStatus.CREATED,
        compensating=True,
    )
    seeded.add(refund)
    seeded.flush()

    body = json.dumps(
        {
            "entity": "event",
            "event": "refund.failed",
            "contains": ["refund"],
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_failed_later",
                        "entity": "refund",
                        "amount": 45000,
                        "status": "failed",
                    }
                }
            },
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )

    assert response.status_code == 200, response.text
    assert response.json()["handled"] == ["refund.failed"]

    assert refund.status == RefundStatus.FAILED

    filed = seeded.scalars(
        select(PaymentException).where(
            PaymentException.correlation_id == "dwc_refund_failed",
            PaymentException.kind == "refund_failed_after_creation",
        )
    ).all()
    assert filed, "a refund that failed after creation must leave an actionable record"
    assert filed[0].local_state["compensating"] is True
    assert filed[0].resolved is False


def test_whatsapp_subscription_handshake(client):
    ok = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.META_VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert ok.status_code == 200
    assert ok.text == "12345"

    refused = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert refused.status_code == 403


def test_whatsapp_webhook_requires_a_valid_signature(client):
    payload = {"object": "whatsapp_business_account", "entry": []}
    body = json.dumps(payload).encode()

    assert client.post("/webhooks/whatsapp", content=body).status_code == 401
    assert (
        client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": _meta_signature(body)}
        ).status_code
        == 200
    )


def test_whatsapp_answer_is_applied_once(client, seeded):
    from app.escalation.service import raise_escalation

    escalation = raise_escalation(
        seeded,
        correlation_id="dwc_api_esc",
        checkout_id="co_api_esc",
        agent_id="agent:api",
        constraint_text="nothing perishable",
        raised_reason="CONSTRAINT_UNRESOLVED",
        amount_minor=1000,
        currency="INR",
        fingerprint="fp",
        cart_summary="1 x thing",
    )
    seeded.flush()

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "919999999999",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": f"dwarpal_approve:{escalation.id}",
                                            "title": "Approve",
                                        },
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _meta_signature(body)}

    first = client.post("/webhooks/whatsapp", content=body, headers=headers).json()
    assert first["applied"][0]["accepted"] is True
    assert first["applied"][0]["status"] == "approved"

    second = client.post("/webhooks/whatsapp", content=body, headers=headers).json()
    assert second["applied"][0]["accepted"] is False
    assert second["applied"][0]["ignored_reason"] == "already_answered"


# --- merchant surface -----------------------------------------------------------------------------


def test_merchant_surfaces_answer(client):
    for path in (
        "/merchant/overview",
        "/merchant/traffic",
        "/merchant/verdicts",
        "/merchant/mandates",
        "/merchant/agents",
        "/merchant/escalations",
        "/merchant/evidence",
        "/merchant/exceptions",
        "/merchant/disputes",
        "/merchant/checkouts",
        "/merchant/reason-codes",
        "/merchant/reports",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:200]}"


def test_reason_codes_are_published_with_their_agent_actions(client):
    body = client.get("/merchant/reason-codes").json()
    published = {entry["code"] for entry in body["codes"]}
    assert published == {code.value for code in ReasonCode}
    for entry in body["codes"]:
        assert entry["agent_action"]
