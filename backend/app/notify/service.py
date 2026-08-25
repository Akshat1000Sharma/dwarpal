"""Deliver a purchase receipt, and record the attempt whatever happens.

Three rules:

    - a delivery failure never fails the checkout. The money decision was already made and
      recorded; a messaging outage must not roll it back.
    - every attempt is logged, including the ones skipped for want of a recipient. A human not
      hearing about a purchase is itself a fact the merchant should be able to show.
    - nothing here can approve anything. A receipt reports; it never asks.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.base import SessionFactory
from app.db.models import (
    AgentConnection,
    NotificationKind,
    NotificationLog,
    NotificationStatus,
)
from app.escalation.whatsapp import WhatsAppTransport, default_transport
from app.logging import get_logger
from app.notify import messages
from app.settings import settings

logger = get_logger(__name__)

# Which outcome of app.checkout.complete carries which receipt. Outcomes not named here get none:
# an escalation is already a message of its own, and awaiting_payment is not yet news.
OUTCOME_KIND: dict[str, NotificationKind] = {
    "completed": NotificationKind.PURCHASE_COMPLETED,
    "refused": NotificationKind.PURCHASE_REFUSED,
    "compensated": NotificationKind.PURCHASE_COMPENSATED,
}


@dataclass(frozen=True)
class Recipient:
    number: str
    connection_id: str | None


def recipient_for(
    session: Session, *, agent_id: str, connection_id: str | None, kind: NotificationKind
) -> Recipient | None:
    """Whose phone this receipt goes to, if anybody's.

    A connection created for this agent wins, because that is the person who pointed the agent
    here and gave a number for exactly this.

    Without one, only an outcome where money actually moved falls back to the configured
    principal. A refusal has no such fallback on purpose: an agent nobody registered is usually an
    unverified caller or an attacker, and messaging the merchant's own phone on every forged
    credential turns a useful notification into an alarm nobody reads.
    """
    connection: AgentConnection | None = None
    if connection_id:
        connection = session.get(AgentConnection, connection_id)
    if connection is None:
        # Two rows here means two people claim this identifier, and picking the newest would send
        # one of them the other's purchase. Derived identifiers carry a unique suffix so this is
        # not reachable by accident; an explicitly supplied duplicate resolves to nobody rather
        # than to a guess.
        candidates = list(
            session.scalars(
                select(AgentConnection)
                .where(
                    AgentConnection.agent_id == agent_id,
                    AgentConnection.revoked_at.is_(None),
                )
                .order_by(desc(AgentConnection.created_at))
                .limit(2)
            ).all()
        )
        if len(candidates) == 1:
            connection = candidates[0]
        elif len(candidates) > 1:
            logger.warning(
                "several live connections claim one agent id; routing no receipt",
                extra={"context": {"agent_id": agent_id}},
            )
            return None
    if connection is not None and connection.whatsapp_e164:
        return Recipient(connection.whatsapp_e164, connection.id)
    if kind is not NotificationKind.PURCHASE_REFUSED and settings.ESCALATION_HUMAN_WHATSAPP:
        return Recipient(settings.ESCALATION_HUMAN_WHATSAPP, None)
    return None


def _wants(connection: AgentConnection | None, kind: NotificationKind) -> bool:
    if connection is None:
        return True
    if kind is NotificationKind.PURCHASE_REFUSED:
        return connection.notify_refused
    return connection.notify_completed


_OUTCOME_TEXT: dict[NotificationKind, str] = {
    NotificationKind.PURCHASE_COMPLETED: "The purchase completed.",
    NotificationKind.PURCHASE_COMPENSATED: (
        "Your authority was withdrawn after payment, so the money was returned."
    ),
}


def _build(
    kind: NotificationKind,
    *,
    to_number: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    agent_id: str,
    reason_code: str,
    reference: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Every route worth trying for this receipt, best first.

    An approved template is preferred when one is configured, because it is the only thing Meta
    delivers to an inbox that has been quiet for more than 24 hours. It can be unavailable for
    reasons outside this code, most often because it is still in review, and telling the human
    nothing at all would be a worse answer than telling them by the route that does work. The
    free-form message only delivers inside the 24 hour window, so it is a fallback and not a
    replacement. This mirrors the escalation path in app/escalation/service.py.
    """
    routes: list[tuple[str, dict[str, Any]]] = []
    merchant = settings.MERCHANT_NAME
    if settings.META_RECEIPT_TEMPLATE_NAME:
        outcome_text = _OUTCOME_TEXT.get(kind, f"The purchase was refused: {reason_code}.")
        payload = messages.build_receipt_template_message(
            to_number=to_number,
            template_name=settings.META_RECEIPT_TEMPLATE_NAME,
            language_code=settings.META_RECEIPT_TEMPLATE_LANGUAGE,
            merchant_name=merchant,
            amount_minor=amount_minor,
            currency=currency,
            cart_summary=cart_summary,
            outcome_text=outcome_text,
            reference=reference,
        )
        routes.append((f"template:{settings.META_RECEIPT_TEMPLATE_NAME}", payload))

    common = {
        "to_number": to_number,
        "merchant_name": merchant,
        "amount_minor": amount_minor,
        "currency": currency,
        "cart_summary": cart_summary,
        "agent_id": agent_id,
        "reference": reference,
    }
    if kind is NotificationKind.PURCHASE_COMPLETED:
        routes.append(("text", messages.build_purchase_receipt_message(**common)))
    elif kind is NotificationKind.PURCHASE_COMPENSATED:
        routes.append(("text", messages.build_purchase_compensated_message(**common)))
    else:
        routes.append(
            ("text", messages.build_purchase_refused_message(**common, reason_code=reason_code))
        )
    return routes


def _record(
    session: Session,
    *,
    correlation_id: str,
    connection_id: str | None,
    kind: NotificationKind,
    route: str,
    to_number: str | None,
    status: NotificationStatus,
    provider_message_id: str | None = None,
    error: str | None = None,
    summary: str = "",
) -> NotificationLog:
    row = NotificationLog(
        correlation_id=correlation_id,
        connection_id=connection_id,
        kind=kind.value,
        route=route,
        to_number=to_number,
        status=status.value,
        provider_message_id=provider_message_id,
        error=(error or "")[:1000] or None,
        summary=summary[:500],
    )
    session.add(row)
    session.flush()
    return row


def notify_outcome(
    session: Session,
    *,
    correlation_id: str,
    outcome: str,
    agent_id: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    reason_code: str,
    connection_id: str | None = None,
    transport: WhatsAppTransport | None = None,
) -> NotificationLog | None:
    """Tell the human what an agent just did with their authority.

    Returns the log row, or None when this outcome carries no receipt. Never raises: the caller is
    a settled money path and must not be unwound by a messaging failure.
    """
    kind = OUTCOME_KIND.get(outcome)
    if kind is None or not settings.NOTIFY_PURCHASE_RECEIPTS:
        return None

    target = recipient_for(session, agent_id=agent_id, connection_id=connection_id, kind=kind)
    if target is None:
        return _record(
            session,
            correlation_id=correlation_id,
            connection_id=connection_id,
            kind=kind,
            route="none",
            to_number=None,
            status=NotificationStatus.SKIPPED,
            error="nobody has registered a WhatsApp number for this agent",
            summary=cart_summary,
        )

    connection = (
        session.get(AgentConnection, target.connection_id) if target.connection_id else None
    )
    if not _wants(connection, kind):
        return _record(
            session,
            correlation_id=correlation_id,
            connection_id=target.connection_id,
            kind=kind,
            route="none",
            to_number=target.number,
            status=NotificationStatus.SKIPPED,
            error="this connection has that receipt switched off",
            summary=cart_summary,
        )

    routes = _build(
        kind,
        to_number=target.number,
        amount_minor=amount_minor,
        currency=currency,
        cart_summary=cart_summary,
        agent_id=agent_id,
        reason_code=reason_code,
        reference=correlation_id,
    )

    sender = transport or default_transport()
    failures: list[str] = []
    for route, payload in routes:
        try:
            response = sender.send(payload)
        except Exception as exc:
            failures.append(f"{route}: {type(exc).__name__}: {exc}")
            logger.warning(
                "purchase receipt route failed",
                extra={
                    "context": {
                        "correlation_id": correlation_id,
                        "kind": kind.value,
                        "route": route,
                    }
                },
            )
            continue

        sent = (response or {}).get("messages") or []
        return _record(
            session,
            correlation_id=correlation_id,
            connection_id=target.connection_id,
            kind=kind,
            route=route,
            to_number=target.number,
            status=NotificationStatus.SENT,
            provider_message_id=str(sent[0].get("id")) if sent else None,
            # Kept even though the human was reached, so a template that never got approved is
            # visible rather than masked by the fallback quietly working.
            error=" | ".join(failures) or None,
            summary=cart_summary,
        )

    logger.warning(
        "purchase receipt could not be delivered on any route",
        extra={"context": {"correlation_id": correlation_id, "kind": kind.value}},
    )
    return _record(
        session,
        correlation_id=correlation_id,
        connection_id=target.connection_id,
        kind=kind,
        route=routes[0][0] if routes else "none",
        to_number=target.number,
        status=NotificationStatus.FAILED,
        error=" | ".join(failures),
        summary=cart_summary,
    )


# One worker, one queue, one database connection. Sending means an HTTP call to Meta, and doing
# that on the request thread makes an agent wait on Meta's latency for a decision already made,
# while holding the rows the next checkout needs. A thread per receipt is worse: under load each
# one takes a connection from the pool and blocks on the same outage until the pool is empty. One
# worker cannot exhaust anything, and a receipt is not latency-critical.
_QUEUE_LIMIT = 256
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_LIMIT)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_drain, name="whatsapp-receipts", daemon=True)
        _worker.start()


def _drain() -> None:
    while True:
        payload = _queue.get()
        session = SessionFactory()
        try:
            _guarded(session, payload)
            session.commit()
        except Exception as exc:
            # Including a failure in the commit itself, which _guarded does not cover. Silence
            # here would mean every receipt failing identically with nothing to read afterwards.
            session.rollback()
            logger.warning(
                "the receipt worker could not record an attempt",
                extra={"context": {"error": f"{type(exc).__name__}: {exc}"}},
            )
        finally:
            session.close()
            _queue.task_done()


def notify_safely(session: Session, **kwargs: Any) -> NotificationLog | None:
    """Send a receipt without letting it touch the transaction that decided the money.

    Under APP_ENV=testing the transport is the recording stub and there is nothing to wait for, so
    it runs inline. That keeps the suite deterministic: a test can assert on the log row the
    moment the call returns.

    Otherwise the send is queued. A full queue means the channel is not keeping up, and the
    receipt is dropped rather than allowed to slow a money decision down. The drop is recorded on
    the caller's session, because a receipt nobody got is exactly the sort of thing a merchant
    should be able to show later.
    """
    if settings.APP_ENV == "testing":
        return _guarded(session, kwargs)

    _ensure_worker()
    try:
        _queue.put_nowait(dict(kwargs))
    except queue.Full:
        logger.warning(
            "purchase receipt dropped: the delivery queue is full",
            extra={"context": {"correlation_id": kwargs.get("correlation_id")}},
        )
        return _record_drop(session, kwargs)
    return None


def _record_drop(session: Session, kwargs: dict[str, Any]) -> NotificationLog | None:
    kind = OUTCOME_KIND.get(str(kwargs.get("outcome")))
    if kind is None:
        return None
    # A savepoint, because this writes on the caller's live money-path session. Without one a
    # failed flush leaves the transaction needing a rollback, and the checkout's own commit then
    # raises PendingRollbackError, unwinding a decision that was already made and recorded.
    try:
        with session.begin_nested():
            return _record(
                session,
                correlation_id=str(kwargs.get("correlation_id", "")),
                connection_id=kwargs.get("connection_id"),
                kind=kind,
                route="none",
                to_number=None,
                status=NotificationStatus.FAILED,
                error="the receipt queue was full and this one was dropped",
                summary=str(kwargs.get("cart_summary", "")),
            )
    except Exception:
        return None


def _guarded(session: Session, kwargs: dict[str, Any]) -> NotificationLog | None:
    try:
        return notify_outcome(session, **kwargs)
    except Exception as exc:
        logger.warning(
            "purchase receipt failed entirely",
            extra={"context": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        return None


def for_correlation(session: Session, correlation_id: str) -> list[NotificationLog]:
    """Every receipt attempt for one purchase.

    Asked for directly rather than filtered out of the newest N rows: after a demo run there are
    thousands of newer notifications, and a slice of them contains none of this purchase's.
    """
    return list(
        session.scalars(
            select(NotificationLog)
            .where(NotificationLog.correlation_id == correlation_id)
            .order_by(NotificationLog.created_at)
        ).all()
    )


def recent(session: Session, limit: int = 50) -> list[NotificationLog]:
    return list(
        session.scalars(
            select(NotificationLog).order_by(desc(NotificationLog.created_at)).limit(limit)
        ).all()
    )
