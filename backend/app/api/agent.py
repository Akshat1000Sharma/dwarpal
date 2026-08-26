"""Agent-facing endpoints: discovery, catalog, policy terms, quote and checkout.

The entire purchase path is completable by a machine. There is no human-facing web UI anywhere in
it, and every refusal is a structured, machine-actionable response.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import agent_identifier, get_db
from app.catalog import discovery, policy_terms
from app.catalog import service as catalog
from app.checkout import idempotency
from app.checkout.complete import complete
from app.checkout.quote import QuoteError, RequestedLine, create_quote, quote_document
from app.correlation import get_correlation_id
from app.errors import AgentError
from app.kernel.reasons import ReasonCode, action_for, is_retryable
from app.keys import merchant_jwks
from app.semantic.client import get_client
from app.settings import settings
from app.verification.pipeline import PresentedCredentials

router = APIRouter(tags=["agent"])


@router.get("/.well-known/ap2-merchant")
def get_discovery(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    return discovery.discovery_document(db)


@router.get("/.well-known/jwks.json")
def get_jwks() -> dict[str, Any]:
    return merchant_jwks()


@router.get("/catalog/items")
def list_items(
    db: Annotated[Session, Depends(get_db)],
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    entries = catalog.browse(db, category=category, limit=limit, offset=offset)
    return {"items": [e.as_document() for e in entries], "count": len(entries)}


@router.get("/catalog/search")
def search_items(
    db: Annotated[Session, Depends(get_db)],
    q: str = Query(min_length=1),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    entries = catalog.search(db, q, limit=limit)
    return {"query": q, "items": [e.as_document() for e in entries], "count": len(entries)}


@router.get("/catalog/categories")
def list_categories(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    return {"categories": catalog.categories(db)}


@router.get("/catalog/items/{sku}")
def get_item(sku: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    entry = catalog.by_sku(db, sku)
    if entry is None:
        raise AgentError(ReasonCode.ITEM_UNKNOWN, f"no item with sku {sku}", detail={"sku": sku})
    return entry.as_document()


@router.get("/policy/terms")
def get_policy_terms(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    return policy_terms.active_terms(db).as_document()


@router.get("/policy/terms/{content_hash}")
def get_policy_terms_by_hash(
    content_hash: str, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    row = policy_terms.terms_by_hash(db, content_hash)
    if row is None:
        raise AgentError(
            ReasonCode.POLICY_HASH_MISMATCH,
            "no policy terms with that content hash",
            detail={"content_hash": content_hash},
        )
    return {
        "content_hash": row.content_hash,
        "media_type": "text/markdown",
        "body": row.body,
        "signed_jwt": row.signed_jwt,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
    }


class QuoteLine(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class QuoteRequest(BaseModel):
    items: list[QuoteLine] = Field(min_length=1)


@router.post("/checkout/quote")
def post_quote(
    body: QuoteRequest,
    db: Annotated[Session, Depends(get_db)],
    agent_id: Annotated[str, Depends(agent_identifier)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    payload = body.model_dump()
    if idempotency_key:
        replay = idempotency.lookup(db, idempotency_key, payload)
        if replay is not None and replay.status_code:
            return replay.response
        if not idempotency.claim(db, idempotency_key, "quote", payload):
            raise AgentError(
                ReasonCode.CHECKOUT_UNKNOWN,
                "a request with this idempotency key is already in flight",
                status_code=409,
            )

    try:
        result = create_quote(
            db,
            agent_id=agent_id,
            correlation_id=get_correlation_id(),
            lines=[RequestedLine(sku=i.sku, quantity=i.quantity) for i in body.items],
        )
    except QuoteError as exc:
        raise AgentError(exc.reason_code, exc.message, detail=exc.detail) from exc

    document = quote_document(result)
    if idempotency_key:
        idempotency.record(db, idempotency_key, status_code=200, response=document)
    return document


@router.get("/checkout/{checkout_id}")
def get_checkout(checkout_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    from app.db.models import CheckoutSession

    row = db.get(CheckoutSession, checkout_id)
    if row is None:
        raise AgentError(
            ReasonCode.CHECKOUT_UNKNOWN, "unknown checkout", detail={"id": checkout_id}
        )
    return {
        "checkout_id": row.id,
        "state": row.state,
        "total": {"amount": row.total_minor, "currency": row.currency},
        "policy_hash": row.policy_hash,
        "expires_at": row.expires_at.isoformat(),
        "checkout": row.checkout,
    }


class CompleteRequest(BaseModel):
    """The four credentials, exactly as AP2 defines them, plus one Dwarpal extension.

    ``presence_attestation`` is optional and selects the human-present flow. Omitting it is the
    human-not-present flow, which is unchanged. Supplying one does not widen anything: it is
    verified like any other credential and recorded on the verdict.
    """

    open_checkout_mandate: str
    closed_checkout_mandate: str
    open_payment_mandate: str | None = None
    closed_payment_mandate: str | None = None
    nonce: str | None = None
    buyer_region: str | None = None
    presence_attestation: str | None = None


@router.post("/checkout/complete")
def post_complete(
    body: CompleteRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    payload = body.model_dump()
    if idempotency_key:
        replay = idempotency.lookup(db, idempotency_key, payload)
        if replay is not None and replay.status_code:
            response.status_code = replay.status_code
            return replay.response
        if not idempotency.claim(db, idempotency_key, "checkout/complete", payload):
            raise AgentError(
                ReasonCode.CHECKOUT_UNKNOWN,
                "a request with this idempotency key is already in flight",
                status_code=409,
            )

    credentials = PresentedCredentials(
        open_checkout=body.open_checkout_mandate,
        closed_checkout=body.closed_checkout_mandate,
        open_payment=body.open_payment_mandate,
        closed_payment=body.closed_payment_mandate,
        nonce=body.nonce,
        presence=body.presence_attestation,
    )
    outcome = complete(
        db,
        credentials,
        correlation_id=get_correlation_id(),
        semantic_client=_semantic_client(),
        audience=settings.PUBLIC_BASE_URL,
        buyer_region=body.buyer_region,
    )

    document: dict[str, Any] = {
        "status": outcome.status,
        "reason_code": outcome.reason_code.value,
        # A policy refusal is returned rather than raised, so the machine-actionable guidance every
        # other refusal carries has to be derived here too. This is the path where money is at
        # stake, so it is the last one that should make an agent parse prose.
        "action": action_for(outcome.reason_code).value,
        "retryable": is_retryable(outcome.reason_code),
        "checkout_id": outcome.checkout_id,
        "correlation_id": get_correlation_id(),
        "evidence_packet_id": outcome.evidence_packet_id,
        "detail": outcome.detail,
    }
    if outcome.receipt is not None:
        document["receipt"] = outcome.receipt
        document["receipt_jwt"] = outcome.receipt_jwt
    if outcome.challenge is not None:
        document["challenge"] = outcome.challenge
    if outcome.refund_id:
        document["refund_id"] = outcome.refund_id

    status_code = 200 if outcome.status in ("completed", "compensated") else outcome.http_status
    response.status_code = status_code
    if idempotency_key:
        idempotency.record(db, idempotency_key, status_code=status_code, response=document)
    return document


def _semantic_client() -> Any:
    """The model is optional at run time; without it every unresolved constraint escalates."""
    try:
        return get_client()
    except Exception:  # an unavailable model must not stop the gate working
        return None


class ConfirmRequest(BaseModel):
    """A present person's answer to an escalation, signed by the surface they are at."""

    escalation_id: str
    confirmation: str


@router.post("/checkout/confirm")
def post_confirm(
    body: ConfirmRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Answer an escalation in band, for a checkout where the person is at the keyboard.

    In the human-not-present flow the question goes to WhatsApp and comes back through the webhook.
    When a person is already at the surface, sending them a message to answer a question they are
    sitting in front of is theatre, so the answer is accepted here instead.

    What is not shortened is the checking. The answer has to be signed by the same trusted surface
    that issued the mandate being spent, be inside its own expiry, and name both this escalation and
    this Checkout. It is then applied through the same escalation service, so the deadline, the
    answered-once rule and the cart binding all still hold. An agent cannot approve its own
    escalation: it holds an agent key, and this accepts one surface's key and no other.
    """
    from app.ap2.jose import JoseError, decode_jws_unverified, public_key_from_jwk, verify_jws
    from app.ap2.vocabulary import CONFIRMATION_JWT_TYP
    from app.db.base import utcnow
    from app.db.models import CheckoutSession, Escalation
    from app.escalation import service as escalation_service
    from app.trust.registry import get_registry

    escalation = db.get(Escalation, body.escalation_id)
    if escalation is None:
        raise AgentError(
            ReasonCode.CHECKOUT_UNKNOWN,
            "no escalation with that id",
            detail={"escalation_id": body.escalation_id},
        )

    try:
        header, unverified = decode_jws_unverified(body.confirmation)
    except JoseError as exc:
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_INVALID, f"malformed confirmation: {exc}"
        ) from exc
    if header.get("typ") != CONFIRMATION_JWT_TYP:
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_INVALID,
            f"confirmation typ must be {CONFIRMATION_JWT_TYP}",
        )

    row = db.get(CheckoutSession, escalation.checkout_id)
    if row is None:
        raise AgentError(
            ReasonCode.CHECKOUT_UNKNOWN,
            "the escalation names a Checkout this merchant does not hold",
        )

    # Only the surface that issued the human's standing authority may answer for them, and it is the
    # one recorded when the question was put, not whatever the Checkout points at now: an escalated
    # Checkout is still presentable, and every presentation rewrites its mandate_id.
    asked_of = escalation.issuer_id
    if not asked_of:
        raise AgentError(
            ReasonCode.PRESENCE_ISSUER_UNTRUSTED,
            "no issuing surface is recorded against this escalation, so nobody can answer for it",
            detail={"escalation_id": escalation.id},
        )

    registry = get_registry()
    issuer_id = str(unverified.get("iss") or "")
    if issuer_id != asked_of:
        raise AgentError(
            ReasonCode.PRESENCE_ISSUER_UNTRUSTED,
            "the confirmation was signed by an authority that did not issue this mandate",
            detail={"signed_by": issuer_id or "nobody", "mandate_issued_by": asked_of},
        )

    keys = registry.keys_for(issuer_id)
    if not keys:
        raise AgentError(
            ReasonCode.PRESENCE_ISSUER_UNTRUSTED,
            f"{issuer_id or 'nobody'} is not a trusted surface",
            detail={"issuer_id": issuer_id},
        )

    claims: dict[str, Any] | None = None
    for key in keys:
        try:
            claims = verify_jws(body.confirmation, public_key_from_jwk(key))
            break
        except JoseError:
            continue
    if claims is None:
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_INVALID, "the confirmation signature is invalid"
        )

    # An answer given an hour ago is not an answer to a question asked now.
    expires_at = claims.get("exp")
    if expires_at is None:
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_INVALID, "the confirmation carries no expiry"
        )
    if int(utcnow().timestamp()) > int(expires_at) + settings.CREDENTIAL_CLOCK_SKEW_SECONDS:
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_STALE,
            "the confirmation has expired",
            detail={"exp": int(expires_at)},
        )

    if claims.get("escalation_id") != escalation.id:
        raise AgentError(
            ReasonCode.PRESENCE_BINDING_MISMATCH, "the confirmation answers a different escalation"
        )
    if claims.get("checkout_hash") != row.checkout_hash:
        raise AgentError(
            ReasonCode.PRESENCE_BINDING_MISMATCH, "the confirmation covers a different Checkout"
        )

    decision = str(claims.get("decision", ""))
    if decision not in ("approve", "deny"):
        raise AgentError(
            ReasonCode.PRESENCE_ATTESTATION_INVALID, "the confirmation carries no usable decision"
        )

    outcome = escalation_service.record_answer(
        db, escalation.id, decision, proof=body.confirmation
    )
    settled = escalation_service.resolve(
        db, escalation.id, current_fingerprint=row.cart_fingerprint
    )
    return {
        "escalation_id": escalation.id,
        "accepted": outcome.accepted,
        "ignored_reason": outcome.ignored_reason,
        "status": settled.status,
        "checkout_id": escalation.checkout_id,
        "next": "present the credential chain again to settle the checkout"
        if outcome.accepted and decision == "approve"
        else "the escalation is closed",
    }
