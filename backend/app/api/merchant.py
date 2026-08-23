"""Merchant-facing endpoints.

This is what the dashboard reads. Agents never touch these; they are the human's view of agent
traffic, verdicts, mandates, evidence, disputes and the generated reports.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.base import utcnow
from app.db.models import (
    AgentIdentity,
    BudgetReservation,
    CheckoutSession,
    Dispute,
    DisputeOutcome,
    Escalation,
    EvidencePacket,
    OpenMandate,
    Payment,
    PaymentException,
    Refund,
    ReservationStatus,
    SpendEvent,
)
from app.db.models import Verdict as VerdictRow
from app.disputes import responder
from app.evidence import locker
from app.kernel import revocation
from app.kernel.reasons import ACTIONS, ReasonCode
from app.settings import settings

router = APIRouter(prefix="/merchant", tags=["merchant"])


def _amount(minor: int, currency: str = "INR") -> dict[str, Any]:
    return {"amount": minor, "currency": currency, "display": f"{currency} {minor / 100:.2f}"}


@router.get("/overview")
def overview(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=24)
    verdicts = db.execute(
        select(VerdictRow.decision, func.count(VerdictRow.id))
        .where(VerdictRow.created_at >= since)
        .group_by(VerdictRow.decision)
    ).all()
    counts = {decision: int(count) for decision, count in verdicts}
    captured = int(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
                Payment.status == "captured", Payment.created_at >= since
            )
        )
        or 0
    )
    refunded = int(
        db.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.created_at >= since
            )
        )
        or 0
    )
    return {
        "window_hours": 24,
        "verdicts": {
            "allow": counts.get("allow", 0),
            "deny": counts.get("deny", 0),
            "escalate": counts.get("escalate", 0),
            "challenge": counts.get("challenge", 0),
            "total": sum(counts.values()),
        },
        "captured": _amount(captured),
        "refunded": _amount(refunded),
        "active_agents": int(db.scalar(select(func.count(AgentIdentity.id))) or 0),
        "open_mandates": int(
            db.scalar(select(func.count(OpenMandate.id)).where(OpenMandate.revoked_at.is_(None)))
            or 0
        ),
        "pending_escalations": int(
            db.scalar(select(func.count(Escalation.id)).where(Escalation.status == "pending")) or 0
        ),
        "open_exceptions": int(
            db.scalar(
                select(func.count(PaymentException.id)).where(PaymentException.resolved.is_(False))
            )
            or 0
        ),
        "evidence_packets": int(db.scalar(select(func.count(EvidencePacket.seq))) or 0),
        "merchant": {"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME},
    }


@router.get("/traffic")
def traffic(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    """Live agent traffic: who is transacting, under whose authority, and against what budget."""
    since = utcnow() - timedelta(seconds=settings.VELOCITY_WINDOW_SECONDS)
    agents = list(db.scalars(select(AgentIdentity).order_by(AgentIdentity.agent_id)).all())
    rows: list[dict[str, Any]] = []
    for agent in agents:
        spend = int(
            db.scalar(
                select(func.coalesce(func.sum(SpendEvent.amount_minor), 0)).where(
                    SpendEvent.agent_id == agent.agent_id, SpendEvent.occurred_at >= since
                )
            )
            or 0
        )
        count = int(
            db.scalar(
                select(func.count(SpendEvent.id)).where(
                    SpendEvent.agent_id == agent.agent_id, SpendEvent.occurred_at >= since
                )
            )
            or 0
        )
        mandates = list(
            db.scalars(
                select(OpenMandate).where(
                    OpenMandate.agent_id == agent.agent_id, OpenMandate.revoked_at.is_(None)
                )
            ).all()
        )
        budget_total = sum(m.cap_minor or 0 for m in mandates)
        budget_used = sum(m.committed_minor for m in mandates)
        last = db.scalar(
            select(VerdictRow)
            .where(VerdictRow.agent_id == agent.agent_id)
            .order_by(desc(VerdictRow.created_at))
            .limit(1)
        )
        rows.append(
            {
                "agent_id": agent.agent_id,
                "display_name": agent.display_name,
                "issuer_id": agent.issuer_id,
                "tier": agent.tier,
                "kill_switch": agent.kill_switch,
                "window_seconds": settings.VELOCITY_WINDOW_SECONDS,
                "window_spend": _amount(spend),
                "window_transactions": count,
                "budget_total": _amount(budget_total),
                "budget_used": _amount(budget_used),
                "budget_remaining": _amount(max(0, budget_total - budget_used)),
                "open_mandates": len(mandates),
                "last_verdict": None
                if last is None
                else {
                    "decision": last.decision,
                    "reason_code": last.reason_code,
                    "at": last.created_at.isoformat(),
                },
            }
        )
    return {"agents": rows}


@router.get("/verdicts")
def verdicts(
    db: Annotated[Session, Depends(get_db)],
    decision: str | None = None,
    reason_code: str | None = None,
    agent_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    statement = select(VerdictRow).order_by(desc(VerdictRow.created_at))
    if decision:
        statement = statement.where(VerdictRow.decision == decision)
    if reason_code:
        statement = statement.where(VerdictRow.reason_code == reason_code)
    if agent_id:
        statement = statement.where(VerdictRow.agent_id == agent_id)
    rows = list(db.scalars(statement.limit(limit).offset(offset)).all())
    total = int(db.scalar(select(func.count(VerdictRow.id))) or 0)
    return {
        "total": total,
        "verdicts": [
            {
                "id": v.id,
                "correlation_id": v.correlation_id,
                "checkout_id": v.checkout_id,
                "agent_id": v.agent_id,
                "action": v.action,
                "decision": v.decision,
                "reason_code": v.reason_code,
                "agent_action": ACTIONS[ReasonCode(v.reason_code)].value,
                "amount": _amount(v.amount_minor, v.currency),
                "evidence": v.evidence,
                "created_at": v.created_at.isoformat(),
            }
            for v in rows
        ],
    }


@router.get("/reason-codes")
def reason_codes() -> dict[str, Any]:
    return {
        "codes": [
            {"code": code.value, "agent_action": action.value}
            for code, action in sorted(ACTIONS.items(), key=lambda kv: kv[0].value)
        ]
    }


@router.get("/mandates")
def mandates(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    rows = list(db.scalars(select(OpenMandate).order_by(desc(OpenMandate.created_at))).all())
    out: list[dict[str, Any]] = []
    for mandate in rows:
        reserved = int(
            db.scalar(
                select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                    BudgetReservation.mandate_id == mandate.id,
                    BudgetReservation.status == ReservationStatus.RESERVED,
                )
            )
            or 0
        )
        out.append(
            {
                "id": mandate.id,
                "kind": mandate.kind,
                "digest": mandate.digest,
                "agent_id": mandate.agent_id,
                "issuer_id": mandate.issuer_id,
                "tier": mandate.tier,
                "constraints": (mandate.claims or {}).get("constraints", []),
                "extension_constraints": (mandate.claims or {}).get("dwarpal_constraints", []),
                "cap": _amount(mandate.cap_minor or 0, mandate.currency),
                "committed": _amount(mandate.committed_minor, mandate.currency),
                "reserved": _amount(reserved, mandate.currency),
                "remaining": _amount(
                    max(0, (mandate.cap_minor or 0) - mandate.committed_minor - reserved),
                    mandate.currency,
                ),
                "use_count": mandate.use_count,
                "expires_at": mandate.expires_at.isoformat() if mandate.expires_at else None,
                "revoked_at": mandate.revoked_at.isoformat() if mandate.revoked_at else None,
                "revoked_reason": mandate.revoked_reason,
                "created_at": mandate.created_at.isoformat(),
            }
        )
    return {"mandates": out}


class RevokeRequest(BaseModel):
    reason: str = "revoked by the merchant on the principal's behalf"


@router.post("/mandates/{mandate_id}/revoke")
def revoke_mandate(
    mandate_id: str, body: RevokeRequest, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    try:
        mandate = revocation.revoke(db, mandate_id, body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": mandate.id,
        "revoked_at": mandate.revoked_at.isoformat() if mandate.revoked_at else None,
        "reason": mandate.revoked_reason,
    }


@router.get("/agents")
def agents(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    rows = list(db.scalars(select(AgentIdentity).order_by(AgentIdentity.agent_id)).all())
    return {
        "agents": [
            {
                "agent_id": a.agent_id,
                "display_name": a.display_name,
                "issuer_id": a.issuer_id,
                "tier": a.tier,
                "kill_switch": a.kill_switch,
                "max_spend_per_window": _amount(a.max_spend_per_window_minor),
                "max_transactions_per_window": a.max_transactions_per_window,
                "allowed_categories": a.allowed_categories or [],
                "blocked_categories": a.blocked_categories or [],
                "created_at": a.created_at.isoformat(),
            }
            for a in rows
        ]
    }


class AgentControls(BaseModel):
    kill_switch: bool | None = None
    max_spend_per_window_minor: int | None = None
    max_transactions_per_window: int | None = None
    allowed_categories: list[str] | None = None
    blocked_categories: list[str] | None = None


@router.patch("/agents/{agent_id}")
def update_agent(
    agent_id: str, body: AgentControls, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    agent = db.scalar(select(AgentIdentity).where(AgentIdentity.agent_id == agent_id))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(agent, field, value)
    db.flush()
    return {
        "agent_id": agent.agent_id,
        "kill_switch": agent.kill_switch,
        "max_spend_per_window": _amount(agent.max_spend_per_window_minor),
        "max_transactions_per_window": agent.max_transactions_per_window,
        "allowed_categories": agent.allowed_categories or [],
        "blocked_categories": agent.blocked_categories or [],
    }


@router.get("/escalations")
def escalations(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    from app.escalation.service import as_evidence

    rows = list(
        db.scalars(select(Escalation).order_by(desc(Escalation.created_at)).limit(100)).all()
    )
    return {"escalations": [as_evidence(db, e) for e in rows]}


@router.get("/evidence")
def evidence_index(
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    packets = locker.recent(db, limit=limit, offset=offset)
    return {
        "chain": locker.verify_chain(db),
        "packets": [
            {
                "seq": p.seq,
                "packet_id": p.packet_id,
                "correlation_id": p.correlation_id,
                "outcome": (p.body or {}).get("outcome"),
                "agent_id": (p.body or {}).get("agent_id"),
                "created_at": p.created_at.isoformat(),
            }
            for p in packets
        ],
    }


@router.get("/evidence/{correlation_id}")
def evidence_detail(
    correlation_id: str, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    packets = locker.for_correlation(db, correlation_id)
    if not packets:
        raise HTTPException(status_code=404, detail="no evidence for that correlation id")
    chain = locker.verify_chain(db)
    return {
        "correlation_id": correlation_id,
        "chain_valid": chain["valid"],
        "chain_problems": [
            p for p in chain["problems"] if p.get("seq") in {x.seq for x in packets}
        ],
        "packets": [
            {
                "seq": p.seq,
                "packet_id": p.packet_id,
                "prev_hash": p.prev_hash,
                "entry_hash": p.entry_hash,
                "signature": p.signature,
                "created_at": p.created_at.isoformat(),
                "body": p.body,
            }
            for p in packets
        ],
    }


@router.get("/exceptions")
def exceptions(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(PaymentException)
            .order_by(desc(PaymentException.created_at))
            .limit(100)
        ).all()
    )
    return {
        "exceptions": [
            {
                "id": e.id,
                "correlation_id": e.correlation_id,
                "payment_id": e.payment_id,
                "kind": e.kind,
                "local_state": e.local_state,
                "gateway_state": e.gateway_state,
                "resolved": e.resolved,
                "created_at": e.created_at.isoformat(),
            }
            for e in rows
        ]
    }


@router.get("/disputes")
def disputes(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    rows = list(db.scalars(select(Dispute).order_by(desc(Dispute.claimed_at)).limit(100)).all())
    return {
        "disputes": [
            {
                "id": d.id,
                "correlation_id": d.correlation_id,
                "claim": d.claim,
                "recommendation": d.recommendation,
                "strength_score": d.strength_score,
                "outcome": d.outcome,
                "claimed_at": d.claimed_at.isoformat(),
                "decided_at": d.decided_at.isoformat() if d.decided_at else None,
            }
            for d in rows
        ]
    }


class DisputeRequest(BaseModel):
    correlation_id: str
    claim: str = "the cardholder states this purchase was not authorised"


@router.post("/disputes")
def create_dispute(body: DisputeRequest, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    row = responder.respond(db, correlation_id=body.correlation_id, claim=body.claim)
    return {"id": row.id, "representment": row.representment}


@router.get("/disputes/{dispute_id}")
def dispute_detail(dispute_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    row = db.get(Dispute, dispute_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown dispute")
    return {
        "id": row.id,
        "correlation_id": row.correlation_id,
        "claim": row.claim,
        "recommendation": row.recommendation,
        "strength_score": row.strength_score,
        "outcome": row.outcome,
        "representment": row.representment,
        "claimed_at": row.claimed_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


class DisputeDecision(BaseModel):
    outcome: DisputeOutcome


@router.post("/disputes/{dispute_id}/decide")
def decide_dispute(
    dispute_id: str, body: DisputeDecision, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    row = db.get(Dispute, dispute_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown dispute")
    row.outcome = body.outcome.value
    row.decided_at = utcnow()
    db.flush()
    return {"id": row.id, "outcome": row.outcome, "decided_at": row.decided_at.isoformat()}


@router.get("/checkouts")
def checkouts(
    db: Annotated[Session, Depends(get_db)], limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(CheckoutSession)
            .order_by(desc(CheckoutSession.created_at))
            .limit(limit)
        ).all()
    )
    return {
        "checkouts": [
            {
                "id": c.id,
                "correlation_id": c.correlation_id,
                "agent_id": c.agent_id,
                "state": c.state,
                "total": _amount(c.total_minor, c.currency),
                "verified": c.verified,
                "created_at": c.created_at.isoformat(),
                "expires_at": c.expires_at.isoformat(),
            }
            for c in rows
        ]
    }


@router.get("/reports")
def reports() -> dict[str, Any]:
    """Serve the generated report artifacts, if they have been produced."""
    directory = settings.resolve("./reports")
    out: dict[str, Any] = {"generated": False, "attack_scorecard": None, "dispute_defence": None}
    artifacts = (
        ("attack_scorecard.json", "attack_scorecard"),
        ("dispute_defence.json", "dispute_defence"),
    )
    for name, key in artifacts:
        path = directory / name
        if path.exists():
            out[key] = json.loads(path.read_text(encoding="utf-8"))
            out["generated"] = True
    return out
