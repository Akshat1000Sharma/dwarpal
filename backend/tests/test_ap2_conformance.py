"""Conformance against the published AP2 JSON Schemas.

Compliance is demonstrated by interoperating with the reference implementation's own definitions,
not asserted in a README. Everything Dwarpal issues and everything it accepts is validated here
against the schemas vendored verbatim from google-agentic-commerce/AP2.
"""

from __future__ import annotations

import json

import pytest

from app.ap2 import sdjwt
from app.ap2.jose import sha256_b64url, verify_jws
from app.ap2.schema_validation import (
    SCHEMA_ROOT,
    SCHEMAS,
    SchemaConformanceError,
    assert_conforms,
    conformance_errors,
)
from app.ap2.vocabulary import AP2_SCHEMA_REVISION, Vct
from app.checkout import quote
from app.checkout.complete import complete
from app.harness import factory
from app.keys import merchant_key

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]


def _strip(claims: dict, *extra: str) -> dict:
    """Drop the envelope claims the AP2 payload schemas do not describe."""
    dropped = {"iss", "sub", "nbf", "dwarpal_constraints", *extra}
    return {k: v for k, v in claims.items() if k not in dropped}


def test_every_declared_schema_is_vendored() -> None:
    for name, relative in SCHEMAS.items():
        path = SCHEMA_ROOT / relative
        assert path.exists(), f"{name} is declared but {relative} is not vendored"
        assert json.loads(path.read_text(encoding="utf-8"))["$schema"]


def test_the_vendored_revision_is_recorded() -> None:
    notice = (SCHEMA_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Apache License" in notice
    assert AP2_SCHEMA_REVISION in notice, "the pinned revision must match the NOTICE"


def test_the_merchant_signed_checkout_conforms(seeded):
    quoted = quote.create_quote(
        seeded,
        agent_id="agent:conformance",
        correlation_id="dwc_conformance",
        lines=[quote.RequestedLine(sku="DWP-TEA-001", quantity=2)],
    )
    assert_conforms("checkout", quoted.row.checkout)

    payload = verify_jws(quoted.checkout_jwt, merchant_key().public_key)
    assert payload["checkout_id"] == quoted.row.id
    assert payload["policy_hash"] == quoted.policy_hash
    # The AP2 binding rule: the hash covers the serialised checkout JWT verbatim.
    assert sha256_b64url(quoted.checkout_jwt.encode("ascii")) == quoted.checkout_hash


def test_every_credential_dwarpal_accepts_conforms(seeded):
    quoted = quote.create_quote(
        seeded,
        agent_id="agent:conformance2",
        correlation_id="dwc_conformance2",
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in CART],
    )
    principals = factory.Principals.create(agent_id="agent:conformance2")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(CART, natural_language=["nothing perishable"]),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
    )
    credentials = presentation.credentials

    pairs = [
        ("open_checkout_mandate", credentials.open_checkout, Vct.OPEN_CHECKOUT_MANDATE),
        ("checkout_mandate", credentials.closed_checkout, Vct.CLOSED_CHECKOUT_MANDATE),
        ("open_payment_mandate", credentials.open_payment, Vct.OPEN_PAYMENT_MANDATE),
        ("payment_mandate", credentials.closed_payment, Vct.CLOSED_PAYMENT_MANDATE),
    ]
    for schema_name, token, expected_vct in pairs:
        claims = sdjwt.verify(
            token,
            principals.issuer.public_jwk()
            if schema_name.startswith("open")
            else principals.agent.public_jwk(),
        ).claims
        assert claims["vct"] == expected_vct.value
        assert_conforms(schema_name, _strip(claims))


def test_the_natural_language_extension_does_not_break_conformance(seeded):
    """The Dwarpal extension lives outside the AP2 constraints array precisely for this reason."""
    principals = factory.Principals.create(agent_id="agent:extension")
    token = factory.build_open_checkout_mandate(
        principals,
        factory.spec_for_cart(CART, natural_language=["nothing perishable", "no gift wrapping"]),
    )
    claims = sdjwt.verify(token, principals.issuer.public_jwk()).claims

    assert claims["dwarpal_constraints"], "the extension must be present"
    assert all(
        c["type"] in ("checkout.allowed_merchants", "checkout.line_items")
        for c in claims["constraints"]
    ), "the AP2 constraints array must contain only AP2 constraint types"
    assert_conforms("open_checkout_mandate", _strip(claims))


def test_the_receipt_dwarpal_issues_conforms(seeded, gateway):
    quoted = quote.create_quote(
        seeded,
        agent_id="agent:receipt",
        correlation_id="dwc_receipt",
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in CART],
    )
    principals = factory.Principals.create(agent_id="agent:receipt")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(CART),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
    )
    outcome = complete(
        seeded, presentation.credentials, correlation_id="dwc_receipt", gateway=gateway
    )
    assert outcome.status == "completed"
    assert_conforms("checkout_receipt", outcome.receipt)
    assert verify_jws(outcome.receipt_jwt, merchant_key().public_key)["status"] == "Success"


@pytest.mark.parametrize(
    ("schema", "payload", "why"),
    [
        (
            "open_checkout_mandate",
            {"vct": "mandate.checkout.open.1", "cnf": {}, "constraints": []},
            "an open Checkout Mandate must contain a line-items constraint",
        ),
        (
            "checkout_mandate",
            {"vct": "mandate.checkout.1", "checkout_jwt": "x"},
            "checkout_hash is required",
        ),
        (
            "payment_mandate",
            {
                "vct": "mandate.payment",
                "transaction_id": "t",
                "payee": {"id": "m", "name": "M"},
                "payment_amount": {"amount": 1, "currency": "INR"},
                "payment_instrument": {"id": "p", "type": "CARD"},
            },
            "the vct must carry its exact version suffix",
        ),
        (
            "checkout_receipt",
            {"status": "Success", "iss": "m", "iat": 1, "reference": "r"},
            "a successful receipt must carry an order_id",
        ),
        (
            "checkout",
            {"id": "c", "line_items": [], "status": "completed", "currency": "INR", "totals": []},
            "links is required",
        ),
    ],
)
def test_non_conformant_payloads_are_rejected(schema: str, payload: dict, why: str) -> None:
    errors = conformance_errors(schema, payload)
    assert errors, f"expected a conformance error because {why}"
    with pytest.raises(SchemaConformanceError):
        assert_conforms(schema, payload)


def test_amounts_are_integer_minor_units(seeded):
    """A float amount is a real interoperability defect, so it must fail conformance."""
    errors = conformance_errors(
        "payment_mandate",
        {
            "vct": "mandate.payment.1",
            "transaction_id": "t",
            "payee": {"id": "m", "name": "M"},
            "payment_amount": {"amount": 45.5, "currency": "INR"},
            "payment_instrument": {"id": "p", "type": "CARD"},
        },
    )
    assert any("amount" in e for e in errors)
