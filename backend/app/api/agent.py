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
from app.kernel.reasons import ReasonCode
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
    """The four credentials, exactly as AP2 defines them."""

    open_checkout_mandate: str
    closed_checkout_mandate: str
    open_payment_mandate: str | None = None
    closed_payment_mandate: str | None = None
    nonce: str | None = None
    buyer_region: str | None = None


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
