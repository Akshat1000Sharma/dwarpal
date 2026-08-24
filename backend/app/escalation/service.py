"""Escalation lifecycle.

Four rules make this safe, and each has a test:

    - the deadline fails closed. An escalation that is never answered is a denial.
    - a single escalation can be answered once. Late and duplicate answers are recorded and ignored.
    - the approval covers exactly the cart it was raised for. If the cart changes at all, the prior
      approval is void.
    - every escalation, the reason it was raised, the answer and the timing enter the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ap2.jose import hash_payload
from app.db.base import utcnow
from app.db.models import Escalation, EscalationResponse, EscalationStatus
from app.escalation.whatsapp import (
    WhatsAppTransport,
    build_approval_message,
    build_approval_template_message,
    default_transport,
)
from app.logging import get_logger
from app.settings import settings

logger = get_logger(__name__)


def cart_fingerprint(*, checkout: dict[str, Any], total_minor: int, policy_hash: str) -> str:
    """What an approval is bound to. Any change to the cart invalidates a prior answer."""
    lines = sorted(
        (str(line.get("id")), str((line.get("item") or {}).get("id")), int(line.get("quantity", 0)))
        for line in checkout.get("line_items", []) or []
    )
    return hash_payload(
        {
            "lines": lines,
            "total_minor": total_minor,
            "currency": checkout.get("currency"),
            "policy_hash": policy_hash,
        }
    )


@dataclass(frozen=True)
class AnswerOutcome:
    accepted: bool
    status: str
    ignored_reason: str | None = None


def raise_escalation(
    session: Session,
    *,
    correlation_id: str,
    checkout_id: str,
    agent_id: str,
    constraint_text: str,
    raised_reason: str,
    amount_minor: int,
    currency: str,
    fingerprint: str,
    cart_summary: str,
    transport: WhatsAppTransport | None = None,
    deadline_seconds: int | None = None,
) -> Escalation:
    """Create the escalation, then attempt delivery.

    Delivery failure does not approve anything. The escalation still exists with its deadline
    running, so a channel outage resolves to a denial rather than to a purchase.
    """
    deadline = deadline_seconds or settings.ESCALATION_DEADLINE_SECONDS
    escalation = Escalation(
        correlation_id=correlation_id,
        checkout_id=checkout_id,
        agent_id=agent_id,
        constraint_text=constraint_text[:2000],
        raised_reason=raised_reason,
        amount_minor=amount_minor,
        currency=currency,
        cart_fingerprint=fingerprint,
        status=EscalationStatus.PENDING,
        deadline_at=utcnow() + timedelta(seconds=deadline),
    )
    session.add(escalation)
    session.flush()

    sender = transport or default_transport()
    recipient = settings.ESCALATION_HUMAN_WHATSAPP
    if not recipient:
        escalation.delivery_error = "ESCALATION_HUMAN_WHATSAPP is not configured"
        session.flush()
        return escalation

    common = {
        "to_number": recipient,
        "escalation_id": escalation.id,
        "merchant_name": settings.MERCHANT_NAME,
        "amount_minor": amount_minor,
        "currency": currency,
        "cart_summary": cart_summary,
        "constraint_text": constraint_text,
    }

    # An approved template is the only thing that reaches a quiet inbox, so it is tried first when
    # one is configured. It can be unavailable for reasons outside this code, most often because it
    # is still awaiting review, and refusing to ask the human at all would be a worse answer than
    # asking them by the route that does work. The free-form message only delivers inside the
    # 24 hour window, so this is a fallback rather than a replacement.
    routes: list[tuple[str, dict[str, Any]]] = []
    if settings.META_TEMPLATE_NAME:
        routes.append((
            f"template:{settings.META_TEMPLATE_NAME}",
            build_approval_template_message(
                **common,
                template_name=settings.META_TEMPLATE_NAME,
                language_code=settings.META_TEMPLATE_LANGUAGE,
            ),
        ))
    routes.append(("interactive", build_approval_message(**common)))

    failures: list[str] = []
    for route, message in routes:
        try:
            response = sender.send(message)
        except Exception as exc:
            failures.append(f"{route}: {type(exc).__name__}: {exc}")
            logger.warning(
                "escalation delivery route failed",
                extra={"context": {"escalation_id": escalation.id, "route": route}},
            )
            continue
        messages = response.get("messages") or []
        if messages:
            escalation.channel_message_id = str(messages[0].get("id"))
        logger.info(
            "escalation delivered",
            extra={"context": {"escalation_id": escalation.id, "route": route}},
        )
        if failures:
            # Kept so an operator can see the preferred route is broken even though the human
            # was reached. A silent fallback would hide a template that never got approved.
            escalation.delivery_error = " | ".join(failures)[:500]
        break
    else:
        escalation.delivery_error = " | ".join(failures)[:500]
        logger.warning(
            "escalation delivery failed on every route; the deadline still applies",
            extra={"context": {"escalation_id": escalation.id}},
        )
    session.flush()
    return escalation


def expire_overdue(session: Session, *, now: datetime | None = None) -> int:
    """Timeouts resolve to denial. This never approves anything."""
    moment = now or utcnow()
    result = session.execute(
        update(Escalation)
        .where(Escalation.status == EscalationStatus.PENDING, Escalation.deadline_at <= moment)
        .values(status=EscalationStatus.TIMED_OUT, answered_at=moment)
    )
    return int(result.rowcount or 0)


def record_answer(
    session: Session,
    escalation_id: str,
    answer: str,
    *,
    message_id: str | None = None,
    now: datetime | None = None,
) -> AnswerOutcome:
    """Apply an answer exactly once.

    The state change is a conditional UPDATE guarded on the row still being pending, so two
    concurrent replies cannot both win. Whatever happens, the attempt is recorded.
    """
    moment = now or utcnow()
    escalation = session.get(Escalation, escalation_id)
    if escalation is None:
        return AnswerOutcome(False, "unknown", "unknown_escalation")

    def log(accepted: bool, reason: str | None) -> None:
        session.add(
            EscalationResponse(
                escalation_id=escalation_id,
                answer=answer,
                accepted=accepted,
                ignored_reason=reason,
                raw_message_id=message_id,
                received_at=moment,
            )
        )
        session.flush()

    if answer not in ("approve", "deny"):
        log(False, "uninterpretable_answer")
        return AnswerOutcome(False, escalation.status, "uninterpretable_answer")

    if escalation.deadline_at <= moment and escalation.status == EscalationStatus.PENDING:
        session.execute(
            update(Escalation)
            .where(Escalation.id == escalation_id, Escalation.status == EscalationStatus.PENDING)
            .values(status=EscalationStatus.TIMED_OUT, answered_at=moment)
        )
        session.flush()
        log(False, "after_deadline")
        return AnswerOutcome(False, EscalationStatus.TIMED_OUT, "after_deadline")

    target = EscalationStatus.APPROVED if answer == "approve" else EscalationStatus.DENIED
    result = session.execute(
        update(Escalation)
        .where(Escalation.id == escalation_id, Escalation.status == EscalationStatus.PENDING)
        .values(status=target, answered_at=moment)
    )
    session.flush()
    if int(result.rowcount or 0) == 0:
        session.refresh(escalation)
        log(False, "already_answered")
        return AnswerOutcome(False, escalation.status, "already_answered")

    log(True, None)
    return AnswerOutcome(True, target)


def resolve(
    session: Session,
    escalation_id: str,
    *,
    current_fingerprint: str | None = None,
    now: datetime | None = None,
) -> Escalation:
    """Read the settled state, applying the deadline and the cart binding.

    An approval whose cart has changed since it was granted is voided here rather than honoured.
    """
    moment = now or utcnow()
    escalation = session.get(Escalation, escalation_id)
    if escalation is None:
        raise LookupError(f"unknown escalation {escalation_id}")

    if escalation.status == EscalationStatus.PENDING and escalation.deadline_at <= moment:
        escalation.status = EscalationStatus.TIMED_OUT
        escalation.answered_at = moment
        session.flush()

    if (
        escalation.status == EscalationStatus.APPROVED
        and current_fingerprint is not None
        and current_fingerprint != escalation.cart_fingerprint
    ):
        escalation.status = EscalationStatus.VOIDED
        session.flush()

    return escalation


def pending_for_checkout(session: Session, checkout_id: str) -> Escalation | None:
    return session.scalar(
        select(Escalation)
        .where(Escalation.checkout_id == checkout_id)
        .order_by(Escalation.created_at.desc())
        .limit(1)
    )


def as_evidence(session: Session, escalation: Escalation) -> dict[str, Any]:
    responses = list(
        session.scalars(
            select(EscalationResponse)
            .where(EscalationResponse.escalation_id == escalation.id)
            .order_by(EscalationResponse.received_at)
        ).all()
    )
    return {
        "escalation_id": escalation.id,
        "raised_reason": escalation.raised_reason,
        "constraint": escalation.constraint_text,
        "amount": {"amount": escalation.amount_minor, "currency": escalation.currency},
        "cart_fingerprint": escalation.cart_fingerprint,
        "status": escalation.status,
        "created_at": escalation.created_at.isoformat(),
        "deadline_at": escalation.deadline_at.isoformat(),
        "answered_at": escalation.answered_at.isoformat() if escalation.answered_at else None,
        "channel_message_id": escalation.channel_message_id,
        "delivery_error": escalation.delivery_error,
        "responses": [
            {
                "answer": r.answer,
                "accepted": r.accepted,
                "ignored_reason": r.ignored_reason,
                "received_at": r.received_at.isoformat(),
            }
            for r in responses
        ],
    }
