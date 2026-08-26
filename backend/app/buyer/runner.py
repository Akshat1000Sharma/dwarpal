"""Drive one buyer-console purchase, logging every step as it happens.

The console is a demonstration surface, not a second purchase path. Everything here goes through
the same modules an external agent reaches over HTTP: `app.checkout.quote` to get the merchant's
signed Checkout, `app.harness.factory` to play the Trusted Surface and sign the four mandates, and
`app.checkout.complete` to be judged. Nothing is short-circuited, so a run in the console and a
run from a shell produce the same verdict for the same cart.

Runs execute on a worker thread with their own session, and the console polls the event log. The
alternative, holding the request open, would show the operator a spinner and then an answer, which
defeats the point of watching an agent work.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.buyer import planner as planning
from app.buyer.planner import BuyerPlan
from app.checkout import quote as quoting
from app.checkout.complete import complete
from app.correlation import new_correlation_id, set_correlation_id
from app.db.base import SessionFactory, utcnow
from app.db.models import BuyerRun, BuyerRunEvent, BuyerRunStatus
from app.harness import factory
from app.logging import get_logger
from app.semantic.check import SemanticClient
from app.settings import settings

logger = get_logger(__name__)

# The console's runs are attributed to their own agent namespace, so demo traffic is obvious in
# the merchant's verdict log rather than mixed in with interop or corpus traffic.
AGENT_PREFIX = "agent:console-"

# Bounded, because every run holds a session for the length of a purchase and the engine pool is
# 20. A thread per request would let a few console calls starve the merchant surface. Queued runs
# wait, which the console shows as a run that has not started yet.
_MAX_CONCURRENT_RUNS = 4
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=_MAX_CONCURRENT_RUNS, thread_name_prefix="buyer-run"
            )
        return _pool


@dataclass
class RunRequest:
    prompt: str
    budget_cap_minor: int | None = None
    natural_language: list[str] | None = None
    connection_id: str | None = None
    agent_id: str | None = None
    # AP2's human-present flow. The console is the one place where a person genuinely is at the
    # surface, so it is the honest place to demonstrate it.
    human_present: bool = False


class RunLog:
    """Append-only view of one run's progress, with per-step timing."""

    def __init__(self, session: Session, run_id: str) -> None:
        self.session = session
        self.run_id = run_id
        self._seq = 0
        self._started: float | None = None

    def start(self) -> None:
        self._started = time.perf_counter()

    def event(
        self,
        step: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> BuyerRunEvent:
        self._seq += 1
        elapsed = None
        if self._started is not None:
            elapsed = int((time.perf_counter() - self._started) * 1000)
        self._started = time.perf_counter()
        row = BuyerRunEvent(
            run_id=self.run_id,
            seq=self._seq,
            level=level,
            step=step,
            message=message[:2000],
            data=data or {},
            duration_ms=elapsed,
        )
        self.session.add(row)
        self.session.flush()
        self.session.commit()
        return row


def create_run(session: Session, request: RunRequest) -> BuyerRun:
    """Record the run before any work starts, so the console has something to poll immediately."""
    correlation = new_correlation_id()
    agent_id = request.agent_id or f"{AGENT_PREFIX}{correlation[-8:]}"
    row = BuyerRun(
        prompt=request.prompt.strip()[:2000],
        agent_id=agent_id,
        connection_id=request.connection_id,
        status=BuyerRunStatus.PLANNING.value,
        correlation_id=correlation,
    )
    session.add(row)
    session.flush()
    return row


def start(run_id: str, agent_id: str, request: RunRequest, *, block: bool = False) -> None:
    """Run the purchase. Threaded by default so the console can watch it happen.

    The agent's identity and its issuing authority are minted and registered on the calling
    thread, before the worker starts, so the trust registry is never mutated while a verification
    on another request is reading it.
    """
    principals = factory.Principals.create(
        agent_id=agent_id, issuer_id=factory.DEFAULT_ISSUER, register=True
    )
    if block:
        _execute(run_id, request, principals)
        return
    _executor().submit(_execute, run_id, request, principals)


def _execute(run_id: str, request: RunRequest, principals: factory.Principals) -> None:
    session = SessionFactory()
    try:
        _drive(session, run_id, request, principals)
        session.commit()
    except Exception as exc:  # a console run must never take the process down
        session.rollback()
        logger.warning(
            "buyer run failed",
            extra={"context": {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}},
        )
        try:
            run = session.get(BuyerRun, run_id)
            if run is not None:
                run.status = BuyerRunStatus.ERROR.value
                run.finished_at = utcnow()
                session.add(
                    BuyerRunEvent(
                        run_id=run_id,
                        seq=999,
                        level="error",
                        step="failed",
                        message=f"{type(exc).__name__}: {exc}",
                        data={"traceback": traceback.format_exc()[-2000:]},
                    )
                )
                session.commit()
        except Exception:
            session.rollback()
    finally:
        session.close()


def _drive(
    session: Session,
    run_id: str,
    request: RunRequest,
    principals: factory.Principals,
) -> None:
    run = session.get(BuyerRun, run_id)
    if run is None:
        return

    set_correlation_id(run.correlation_id)
    log = RunLog(session, run_id)
    log.start()

    log.event(
        "identity",
        f"Agent identity minted and its issuing authority published: {run.agent_id}",
        data={"issuer": principals.issuer_id, "agent_id": run.agent_id},
    )

    # ---- plan -------------------------------------------------------------------------------
    chosen = planning.get_planner()
    document = planning.catalog_for_planning(session)
    log.event(
        "catalog",
        f"Read {len(document)} catalog items with their purchase constraints",
        data={"items": len(document), "planner": chosen.name},
    )

    try:
        proposed = chosen.propose(run.prompt, document)
    except Exception as exc:
        # A model that is rate limited, unreachable or misbehaving must cost the buyer a less
        # clever cart, never the whole purchase. This mirrors the merchant's own rule about the
        # model: it is consulted, and its absence changes the answer without stopping the work.
        logger.warning(
            "the buyer planner failed; falling back to the deterministic one",
            extra={"context": {"planner": chosen.name, "error": f"{type(exc).__name__}: {exc}"}},
        )
        log.event(
            "planner_fallback",
            (
                f"The {chosen.name} planner was unavailable, so the deterministic catalog planner "
                "chose the cart instead."
            ),
            level="warn",
            data={"planner": chosen.name, "error": str(exc)[:400]},
        )
        chosen = planning.RuleBasedPlanner()
        proposed = chosen.propose(run.prompt, document)

    plan = planning.validate(
        session,
        proposed,
        planner_name=chosen.name,
        hard_cap_minor=request.budget_cap_minor or None,
    )
    for text in request.natural_language or []:
        cleaned = str(text).strip()[:300]
        if cleaned and cleaned not in plan.natural_language:
            plan.natural_language.append(cleaned)

    run.planner = plan.planner
    run.plan = plan.as_dict()
    session.flush()

    if not plan.lines:
        log.event(
            "plan",
            "The agent found nothing in the catalog matching that request, so it bought nothing.",
            level="warn",
            data=plan.as_dict(),
        )
        _finish(session, run, BuyerRunStatus.REFUSED, reason_code="ITEM_UNKNOWN")
        return

    log.event(
        "plan",
        _describe_plan(plan),
        data=plan.as_dict(),
    )

    # ---- the human's standing authority -----------------------------------------------------
    run.status = BuyerRunStatus.QUOTING.value
    session.flush()

    spec = factory.spec_for_cart(
        plan.lines,
        amount_cap_minor=plan.budget_cap_minor,
        natural_language=plan.natural_language,
        budget_minor=plan.budget_cap_minor,
    )
    issued = factory.issue_open_mandates(principals, spec)
    log.event(
        "open_mandates",
        (
            "The trusted surface signed the two open mandates: what may be bought, and how it may "
            f"be paid, capped at {plan.budget_cap_minor / 100:,.2f}"
        ),
        data={
            "open_checkout_mandate_chars": len(issued.open_checkout),
            "open_payment_mandate_chars": len(issued.open_payment),
            "digest": issued.open_checkout_digest,
            "cap_minor": plan.budget_cap_minor,
            "natural_language": plan.natural_language,
        },
    )

    # ---- quote ------------------------------------------------------------------------------
    try:
        quoted = quoting.create_quote(
            session,
            agent_id=run.agent_id,
            correlation_id=run.correlation_id,
            lines=[
                quoting.RequestedLine(sku=sku, quantity=qty) for sku, _title, qty in plan.lines
            ],
        )
    except quoting.QuoteError as exc:
        log.event(
            "quote",
            f"The merchant refused to quote: {exc.message}",
            level="error",
            data={"reason_code": exc.reason_code.value, "detail": exc.detail},
        )
        _finish(session, run, BuyerRunStatus.REFUSED, reason_code=exc.reason_code.value)
        return

    run.checkout_id = quoted.row.id
    run.amount_minor = quoted.row.total_minor
    run.currency = quoted.row.currency
    session.flush()
    log.event(
        "quote",
        (
            f"The merchant froze prices, held stock, and signed a Checkout for "
            f"{quoted.row.currency} {quoted.row.total_minor / 100:,.2f}"
        ),
        data={
            "checkout_id": quoted.row.id,
            "total_minor": quoted.row.total_minor,
            "policy_hash": quoted.policy_hash,
            "checkout_hash": quoted.checkout_hash,
            "expires_at": quoted.row.expires_at.isoformat(),
        },
    )

    # ---- the agent's claim about this purchase ----------------------------------------------
    run.status = BuyerRunStatus.PRESENTING.value
    session.flush()

    presentation = factory.present_issued(
        issued,
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        audience=settings.PUBLIC_BASE_URL,
        nonce=f"console-{run.correlation_id[-12:]}",
        human_present=request.human_present,
    )
    credentials = presentation.credentials
    if request.human_present:
        log.event(
            "presence_attested",
            "The trusted surface attested that a person was at it for this exact cart",
            data={
                "flow": "human-present",
                "bound_to_checkout_hash": quoted.checkout_hash[:16] + "...",
                "valid_for_seconds": settings.PRESENCE_MAX_AGE_SECONDS,
                "note": "verified like any other credential, and it widens nothing",
            },
        )
    log.event(
        "closed_mandates",
        "The agent signed the two closed mandates and proved it holds the key they were issued to",
        data={
            "closed_checkout_mandate_chars": len(credentials.closed_checkout),
            "closed_payment_mandate_chars": len(credentials.closed_payment or ""),
            "nonce": credentials.nonce,
            "audience": settings.PUBLIC_BASE_URL,
        },
    )

    # ---- the merchant decides ---------------------------------------------------------------
    outcome = complete(
        session,
        credentials,
        correlation_id=run.correlation_id,
        semantic_client=_semantic_client(),
        audience=settings.PUBLIC_BASE_URL,
        connection_id=run.connection_id,
    )
    session.flush()

    run.reason_code = outcome.reason_code.value
    run.evidence_packet_id = outcome.evidence_packet_id
    run.payment_id = outcome.payment_id
    run.razorpay_order_id = (outcome.detail or {}).get("razorpay_order_id")
    session.flush()

    log.event(
        "verdict",
        _describe_outcome(outcome.status, outcome.reason_code.value),
        level="info" if outcome.status in ("completed", "awaiting_payment") else "warn",
        data={
            "status": outcome.status,
            "reason_code": outcome.reason_code.value,
            "http_status": outcome.http_status,
            "evidence_packet_id": outcome.evidence_packet_id,
            "detail": outcome.detail,
            "challenge": outcome.challenge,
        },
    )

    status = {
        "completed": BuyerRunStatus.COMPLETED,
        "awaiting_payment": BuyerRunStatus.AWAITING_PAYMENT,
        "compensated": BuyerRunStatus.COMPENSATED,
        "escalated": BuyerRunStatus.ESCALATED,
    }.get(outcome.status, BuyerRunStatus.REFUSED)

    if status is BuyerRunStatus.AWAITING_PAYMENT:
        log.event(
            "payment_required",
            (
                "Authority accepted and an order created. Pay it with the Razorpay test card to "
                "finish the purchase."
            ),
            data={
                "razorpay_order_id": run.razorpay_order_id,
                "amount_minor": run.amount_minor,
                "currency": run.currency,
            },
        )

    _finish(session, run, status, reason_code=outcome.reason_code.value, close=False)


def _finish(
    session: Session,
    run: BuyerRun,
    status: BuyerRunStatus,
    *,
    reason_code: str | None = None,
    close: bool = True,
) -> None:
    run.status = status.value
    if reason_code:
        run.reason_code = reason_code
    # A run awaiting payment is not finished; the operator still has to pay it.
    if close or status is not BuyerRunStatus.AWAITING_PAYMENT:
        run.finished_at = utcnow()
    session.flush()
    session.commit()


def _describe_plan(plan: BuyerPlan) -> str:
    items = ", ".join(f"{qty} x {title}" for _sku, title, qty in plan.lines)
    suffix = ""
    if plan.natural_language:
        suffix = f'. Standing instructions carried through: "{"; ".join(plan.natural_language)}"'
    return f"The agent chose {items}{suffix}"


def _describe_outcome(status: str, reason_code: str) -> str:
    return {
        "completed": "Approved by the policy kernel and captured. Money moved.",
        "awaiting_payment": "Approved by the policy kernel. The order is waiting to be paid.",
        "compensated": "Captured, then the authority was withdrawn, so the money was returned.",
        "escalated": "The kernel could not decide alone and asked the human.",
    }.get(status, f"Refused by the merchant: {reason_code}")


def _semantic_client() -> SemanticClient | None:
    """The model is optional; without it every unresolved constraint escalates.

    Under APP_ENV=testing the offline keyword client stands in, so a console run in the suite
    never reaches Gemini and never depends on a key.
    """
    if settings.APP_ENV == "testing":
        from app.semantic.client import KeywordSemanticClient

        return KeywordSemanticClient()
    try:
        from app.semantic.client import get_client

        return get_client()
    except Exception:
        return None


def events_for(session: Session, run_id: str) -> list[BuyerRunEvent]:
    return list(
        session.scalars(
            select(BuyerRunEvent)
            .where(BuyerRunEvent.run_id == run_id)
            .order_by(BuyerRunEvent.seq)
        ).all()
    )
