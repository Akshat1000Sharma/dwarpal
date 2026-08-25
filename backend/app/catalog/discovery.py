"""The discovery document served at a stable well-known location.

An arriving agent reads this to learn what this merchant is, which protocol versions it speaks,
which credential types it accepts, where to browse, quote and check out, and which trust anchors
it recognises. Everything an agent needs to transact is reachable from here without a human.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ap2.vocabulary import (
    AP2_PROTOCOL_VERSION,
    AP2_SCHEMA_REVISION,
    AP2_SPEC_URL,
    NATURAL_LANGUAGE_CONSTRAINT,
    CheckoutConstraint,
    PaymentConstraint,
    Vct,
)
from app.catalog import policy_terms
from app.settings import settings
from app.trust.registry import get_registry

WELL_KNOWN_PATH = "/.well-known/ap2-merchant"


def _url(path: str) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}"


def discovery_document(session: Session) -> dict[str, Any]:
    registry = get_registry()
    terms = policy_terms.active_terms(session)
    unverified = registry.unverified()

    return {
        "merchant": {
            "id": settings.MERCHANT_ID,
            "name": settings.MERCHANT_NAME,
            "website": settings.MERCHANT_WEBSITE,
        },
        "protocols": {
            "ap2": {
                "version": AP2_PROTOCOL_VERSION,
                "specification": AP2_SPEC_URL,
                "schema_revision": AP2_SCHEMA_REVISION,
                "flows": ["human-not-present"],
            }
        },
        "roles_implemented": ["merchant", "merchant_payment_processor"],
        "roles_mocked": ["credential_provider"],
        "accepted_credentials": [
            {
                "vct": Vct.OPEN_CHECKOUT_MANDATE.value,
                "format": "dc+sd-jwt",
                "signed_by": "human, via a trusted surface",
                "required": True,
            },
            {
                "vct": Vct.CLOSED_CHECKOUT_MANDATE.value,
                "format": "dc+sd-jwt",
                "signed_by": "agent",
                "required": True,
                "key_binding": "required",
            },
            {
                "vct": Vct.OPEN_PAYMENT_MANDATE.value,
                "format": "dc+sd-jwt",
                "signed_by": "human, via a trusted surface",
                "required": True,
            },
            {
                "vct": Vct.CLOSED_PAYMENT_MANDATE.value,
                "format": "dc+sd-jwt",
                "signed_by": "agent",
                "required": True,
                "key_binding": "required",
            },
        ],
        "supported_constraints": {
            "deterministic": sorted(
                [*(c.value for c in CheckoutConstraint), *(c.value for c in PaymentConstraint)]
            ),
            "natural_language": [NATURAL_LANGUAGE_CONSTRAINT],
        },
        "signature": {"alg": "ES256", "crv": "P-256", "jwks_uri": _url("/.well-known/jwks.json")},
        # The value an agent must put in the aud claim of its key-binding JWT. Published because
        # an agent cannot guess it, and a mismatch is refused as a key-binding failure.
        "audience": settings.PUBLIC_BASE_URL,
        "endpoints": {
            "catalog_browse": _url("/catalog/items"),
            "catalog_search": _url("/catalog/search"),
            "catalog_item": _url("/catalog/items/{sku}"),
            "categories": _url("/catalog/categories"),
            "policy_terms": _url("/policy/terms"),
            "quote": _url("/checkout/quote"),
            "checkout_complete": _url("/checkout/complete"),
            "checkout_status": _url("/checkout/{checkout_id}"),
            **({"mcp": settings.MCP_PUBLIC_URL} if settings.MCP_PUBLIC_URL else {}),
        },
        "policy": {
            "current_hash": terms.content_hash,
            "acknowledgment_required": True,
            "acknowledgment_claim": "policy_hash",
        },
        "trust_anchors": registry.trust_anchors(),
        "unverified_access": {
            "description": (
                "An agent that cannot present acceptable credentials may browse, search, quote "
                "and hold stock. It may check out only below the ceiling below, and may not "
                "purchase restricted or age restricted items at any value."
            ),
            "ceiling": {
                "amount": min(settings.UNVERIFIED_CEILING_MINOR, unverified.max_transaction_minor),
                "currency": "INR",
            },
            "challenge_status": 402,
        },
        "limits": {
            "clock_skew_tolerance_seconds": settings.CREDENTIAL_CLOCK_SKEW_SECONDS,
            "inventory_hold_ttl_seconds": settings.INVENTORY_HOLD_TTL_SECONDS,
            "inventory_hold_quota_per_agent": settings.INVENTORY_HOLD_QUOTA_PER_AGENT,
            "budget_reservation_ttl_seconds": settings.BUDGET_RESERVATION_TTL_SECONDS,
            "escalation_deadline_seconds": settings.ESCALATION_DEADLINE_SECONDS,
        },
    }
