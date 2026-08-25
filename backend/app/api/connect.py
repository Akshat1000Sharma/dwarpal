"""Connection management: how somebody registers their own agent against this merchant.

Guarded by the merchant surface, because creating a connection decides where purchase receipts go
and issues a token. The connection itself grants no purchasing authority.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_merchant_token
from app.connect import service as connections
from app.db.models import ConnectionScope
from app.settings import settings

router = APIRouter(
    prefix="/merchant/connections",
    tags=["merchant"],
    dependencies=[Depends(require_merchant_token)],
)


class ConnectionRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    scope: ConnectionScope = ConnectionScope.BUYER
    whatsapp: str | None = None
    agent_id: str | None = None


@router.get("")
def list_connections(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    return {
        "connections": [connections.as_document(c) for c in connections.listing(db)],
        "header": "X-Dwarpal-Connection",
        "public_base_url": settings.PUBLIC_BASE_URL.rstrip("/"),
    }


@router.post("")
def create(
    body: ConnectionRequest, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    """Mint a connection. The token is in this response and nowhere else, ever again."""
    try:
        created = connections.create_connection(
            db,
            label=body.label,
            scope=body.scope,
            whatsapp=body.whatsapp,
            agent_id=body.agent_id,
        )
    except connections.ConnectionError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return connections.as_document(created.connection, token=created.token)


@router.post("/{connection_id}/revoke")
def revoke(connection_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    try:
        row = connections.revoke(db, connection_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return connections.as_document(row)

