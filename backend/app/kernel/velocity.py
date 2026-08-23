"""Velocity limits, category gates and structuring detection.

Structuring is the case worth stating plainly: an agent must not evade a per-transaction cap by
splitting a purchase into several smaller ones. Rolling per-agent and per-mandate aggregates are
kept over configurable windows, and the aggregate is refused when it breaches the very cap the
splits were designed to dodge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import AgentIdentity, SpendEvent
from app.settings import settings


@dataclass(frozen=True)
class WindowUsage:
    spend_minor: int
    transaction_count: int
    window_seconds: int


@dataclass(frozen=True)
class StructuringFinding:
    detected: bool
    window_spend_minor: int
    per_transaction_cap_minor: int
    transaction_count: int


def record_spend(
    session: Session,
    *,
    agent_id: str,
    mandate_id: str | None,
    correlation_id: str,
    amount_minor: int,
    currency: str = "INR",
    occurred_at: datetime | None = None,
) -> SpendEvent:
    event = SpendEvent(
        agent_id=agent_id,
        mandate_id=mandate_id,
        correlation_id=correlation_id,
        amount_minor=amount_minor,
        currency=currency,
        occurred_at=occurred_at or utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def usage_for_agent(
    session: Session,
    agent_id: str,
    *,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> WindowUsage:
    window = window_seconds if window_seconds is not None else settings.VELOCITY_WINDOW_SECONDS
    since = (now or utcnow()) - timedelta(seconds=window)
    row = session.execute(
        select(
            func.coalesce(func.sum(SpendEvent.amount_minor), 0),
            func.count(SpendEvent.id),
        ).where(SpendEvent.agent_id == agent_id, SpendEvent.occurred_at >= since)
    ).one()
    return WindowUsage(
        spend_minor=int(row[0] or 0),
        transaction_count=int(row[1] or 0),
        window_seconds=window,
    )


def usage_for_mandate(
    session: Session,
    mandate_id: str,
    *,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> WindowUsage:
    window = window_seconds if window_seconds is not None else settings.VELOCITY_WINDOW_SECONDS
    since = (now or utcnow()) - timedelta(seconds=window)
    row = session.execute(
        select(
            func.coalesce(func.sum(SpendEvent.amount_minor), 0),
            func.count(SpendEvent.id),
        ).where(SpendEvent.mandate_id == mandate_id, SpendEvent.occurred_at >= since)
    ).one()
    return WindowUsage(
        spend_minor=int(row[0] or 0),
        transaction_count=int(row[1] or 0),
        window_seconds=window,
    )


def detect_structuring(
    session: Session,
    *,
    agent_id: str,
    mandate_id: str | None,
    pending_amount_minor: int,
    per_transaction_cap_minor: int,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> StructuringFinding:
    """Refuse when the windowed total breaches the per-transaction cap.

    The cap expresses how much the human was willing to see move in one go. Several transactions
    that individually sit under it but together exceed it are the evasion this catches.
    """
    window = window_seconds if window_seconds is not None else settings.STRUCTURING_WINDOW_SECONDS
    moment = now or utcnow()
    since = moment - timedelta(seconds=window)

    statement = select(
        func.coalesce(func.sum(SpendEvent.amount_minor), 0), func.count(SpendEvent.id)
    ).where(SpendEvent.occurred_at >= since)
    statement = (
        statement.where(SpendEvent.mandate_id == mandate_id)
        if mandate_id
        else statement.where(SpendEvent.agent_id == agent_id)
    )
    prior_spend, prior_count = session.execute(statement).one()

    total = int(prior_spend or 0) + pending_amount_minor
    count = int(prior_count or 0) + 1
    # A single transaction over the cap is a cap breach, handled by the constraint evaluator. This
    # only fires when the breach is assembled out of several transactions.
    detected = count > 1 and total > per_transaction_cap_minor > 0
    return StructuringFinding(
        detected=detected,
        window_spend_minor=total,
        per_transaction_cap_minor=per_transaction_cap_minor,
        transaction_count=count,
    )


@dataclass(frozen=True)
class AgentGateResult:
    allowed: bool
    reason: str | None = None
    detail: dict[str, object] | None = None


def check_agent_controls(
    session: Session,
    agent: AgentIdentity | None,
    *,
    amount_minor: int,
    categories: set[str],
    now: datetime | None = None,
) -> AgentGateResult:
    """Merchant-set per-agent limits: kill switch, category gates, spend and count per window."""
    if agent is None:
        return AgentGateResult(allowed=True)

    if agent.kill_switch:
        return AgentGateResult(False, "kill_switch", {"agent_id": agent.agent_id})

    blocked = set(agent.blocked_categories or [])
    hit = sorted(categories & blocked)
    if hit:
        return AgentGateResult(False, "category_blocked", {"categories": hit})

    allowed_list = set(agent.allowed_categories or [])
    if allowed_list:
        outside = sorted(categories - allowed_list)
        if outside:
            return AgentGateResult(False, "category_not_allowed", {"categories": outside})

    usage = usage_for_agent(session, agent.agent_id, now=now)
    if usage.spend_minor + amount_minor > agent.max_spend_per_window_minor:
        return AgentGateResult(
            False,
            "spend_window",
            {
                "window_spend_minor": usage.spend_minor,
                "pending_minor": amount_minor,
                "limit_minor": agent.max_spend_per_window_minor,
                "window_seconds": usage.window_seconds,
            },
        )
    if usage.transaction_count + 1 > agent.max_transactions_per_window:
        return AgentGateResult(
            False,
            "count_window",
            {
                "window_count": usage.transaction_count,
                "limit": agent.max_transactions_per_window,
                "window_seconds": usage.window_seconds,
            },
        )
    return AgentGateResult(allowed=True)
