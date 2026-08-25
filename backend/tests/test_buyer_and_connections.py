"""The buyer console, connections, and the purchase receipts they route.

Nothing here contacts Meta, Gemini or Razorpay. The WhatsApp transport is the recording stub, the
planner is the deterministic one, and the gateway is the in-memory stub, all selected by
APP_ENV=testing rather than by anything a test has to remember to pass.
"""

from __future__ import annotations

import hashlib
import hmac
import os

import pytest
from sqlalchemy.orm import Session

from app.buyer import planner as planning
from app.buyer import runner
from app.catalog import service as catalog_service
from app.connect import service as connections
from app.db.models import (
    AgentConnection,
    BuyerRunStatus,
    ConnectionScope,
    NotificationKind,
    NotificationStatus,
)
from app.escalation.whatsapp import RecordingTransport
from app.notify import service as receipts
from app.payments.gateway import verify_checkout_signature


def test_the_suite_forces_its_own_profile() -> None:
    """A developer's .env must not be able to point the suite at a real service.

    APP_ENV=testing is what selects the recording WhatsApp transport, the deterministic buyer
    planner and the stub payment gateway. Before conftest forced it, a local .env carrying
    APP_ENV=development made the suite try to reach Gemini, which passed in CI and failed on a
    developer's machine.
    """
    from app.settings import settings

    assert os.environ["APP_ENV"] == "testing"
    assert settings.APP_ENV == "testing"
    assert settings.DB_NAME.endswith("_test")
    assert settings.MERCHANT_SIGNING_KEY_DIR.endswith("_test")


# --------------------------------------------------------------------------- connections


def test_the_token_is_never_stored_in_the_clear(db: Session) -> None:
    created = connections.create_connection(db, label="My Claude", whatsapp="+919876543210")

    assert created.token.startswith(connections.TOKEN_PREFIX)
    assert created.connection.token_hash != created.token
    assert created.connection.token_hash == hashlib.sha256(created.token.encode()).hexdigest()
    # The plaintext must appear nowhere on the row, under any column.
    for column in AgentConnection.__table__.columns:
        stored = getattr(created.connection, column.name)
        assert stored != created.token


def test_a_connection_resolves_until_it_is_revoked(db: Session) -> None:
    created = connections.create_connection(db, label="Shopper", whatsapp="+919876543210")
    token = created.token

    assert connections.resolve(db, token) is not None
    connections.revoke(db, created.connection.id)
    assert connections.resolve(db, token) is None, "revocation must take effect immediately"


def test_scope_is_enforced_on_resolution(db: Session) -> None:
    buyer = connections.create_connection(db, label="Buyer", scope=ConnectionScope.BUYER)
    seller = connections.create_connection(db, label="Seller", scope=ConnectionScope.MERCHANT)

    assert connections.resolve(db, buyer.token, scope=ConnectionScope.BUYER) is not None
    assert connections.resolve(db, buyer.token, scope=ConnectionScope.MERCHANT) is None
    assert connections.resolve(db, seller.token, scope=ConnectionScope.MERCHANT) is not None
    assert connections.resolve(db, seller.token, scope=ConnectionScope.BUYER) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91 98765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+1 (555) 010-9999", "+15550109999"),
    ],
)
def test_numbers_are_normalised_to_e164(raw: str, expected: str) -> None:
    assert connections.normalise_number(raw) == expected


@pytest.mark.parametrize("raw", ["not a number", "+0123", "+", "12"])
def test_a_number_that_would_never_deliver_is_refused(raw: str) -> None:
    with pytest.raises(connections.ConnectionError_):
        connections.normalise_number(raw)


def test_a_number_is_never_returned_in_full(db: Session) -> None:
    created = connections.create_connection(db, label="Shopper", whatsapp="+919876543210")
    document = connections.as_document(created.connection)

    assert document["whatsapp"] != "+919876543210"
    assert document["whatsapp"].endswith("3210")
    assert "token" not in document, "the token appears only in the response that minted it"


# --------------------------------------------------------------------------- receipts


def _notify(db: Session, transport: RecordingTransport, outcome: str, **overrides: object) -> object:
    payload = {
        "correlation_id": "dwc_receipt_test",
        "outcome": outcome,
        "agent_id": "agent:receipt-test",
        "amount_minor": 145_000,
        "currency": "INR",
        "cart_summary": "2 x Nilgiri Black Tea 250g",
        "reason_code": "APPROVED",
    }
    payload.update(overrides)
    return receipts.notify_outcome(db, transport=transport, **payload)  # type: ignore[arg-type]


def test_a_completed_purchase_tells_the_human_what_was_bought(db: Session) -> None:
    connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    transport = RecordingTransport()

    row = _notify(db, transport, "completed")

    assert row is not None and row.status == NotificationStatus.SENT.value
    assert row.kind == NotificationKind.PURCHASE_COMPLETED.value
    assert len(transport.sent) == 1
    body = transport.sent[0]["text"]["body"]
    assert "completed a purchase on your behalf" in body
    assert "Nilgiri Black Tea" in body
    assert "INR 1,450.00" in body


def test_a_refusal_tells_the_human_no_money_moved(db: Session) -> None:
    connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    transport = RecordingTransport()

    row = _notify(db, transport, "refused", reason_code="BUDGET_EXCEEDED")

    assert row is not None and row.kind == NotificationKind.PURCHASE_REFUSED.value
    body = transport.sent[0]["text"]["body"]
    assert "was refused" in body
    assert "BUDGET_EXCEEDED" in body
    assert "No money moved" in body


def test_a_receipt_goes_to_the_connection_that_owns_the_agent(db: Session) -> None:
    connections.create_connection(
        db, label="Someone else", whatsapp="+911111111111", agent_id="agent:not-this-one"
    )
    mine = connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    transport = RecordingTransport()

    row = _notify(db, transport, "completed")

    assert row is not None and row.connection_id == mine.connection.id
    assert transport.sent[0]["to"] == "+919876543210"


def test_a_connection_can_switch_refusal_receipts_off(db: Session) -> None:
    created = connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    created.connection.notify_refused = False
    db.flush()
    transport = RecordingTransport()

    row = _notify(db, transport, "refused", reason_code="BUDGET_EXCEEDED")

    assert row is not None and row.status == NotificationStatus.SKIPPED.value
    assert transport.sent == [], "a switched-off receipt must not be sent"


def test_an_outcome_with_no_receipt_sends_nothing(db: Session) -> None:
    transport = RecordingTransport()
    assert _notify(db, transport, "awaiting_payment") is None
    assert _notify(db, transport, "escalated") is None
    assert transport.sent == []


def test_a_delivery_failure_is_recorded_and_never_raised(db: Session) -> None:
    connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    broken = RecordingTransport(fail_with=RuntimeError("Meta is down"))

    row = _notify(db, broken, "completed")

    assert row is not None and row.status == NotificationStatus.FAILED.value
    assert "Meta is down" in (row.error or "")


def test_a_receipt_failure_cannot_break_the_caller(db: Session) -> None:
    """notify_safely is what the settled money paths call, so it must swallow everything."""

    class Exploding:
        def send(self, payload: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("boom")

    connections.create_connection(
        db, label="Mine", whatsapp="+919876543210", agent_id="agent:receipt-test"
    )
    result = receipts.notify_safely(
        db,
        correlation_id="dwc_safe",
        outcome="completed",
        agent_id="agent:receipt-test",
        amount_minor=100,
        currency="INR",
        cart_summary="x",
        reason_code="APPROVED",
        transport=Exploding(),
    )
    assert result is not None and result.status == NotificationStatus.FAILED.value


# --------------------------------------------------------------------------- the planner


def test_the_planner_never_puts_an_invented_sku_on_the_wire(seeded: Session) -> None:
    """Model output is a suggestion. validate() is what decides what is actually bought."""
    proposed = planning.PlannedCart(
        lines=[
            planning.PlannedLine(sku="DWP-TEA-001", quantity=2),
            planning.PlannedLine(sku="DWP-DOES-NOT-EXIST", quantity=1),
        ]
    )

    plan = planning.validate(seeded, proposed, planner_name="test")

    assert [sku for sku, _t, _q in plan.lines] == ["DWP-TEA-001"]
    assert any("no such item" in d["why"] for d in plan.dropped)


def test_a_quantity_beyond_the_items_own_limit_is_clamped(seeded: Session) -> None:
    proposed = planning.PlannedCart(lines=[planning.PlannedLine(sku="DWP-TEA-001", quantity=99)])

    plan = planning.validate(seeded, proposed, planner_name="test")

    assert plan.lines[0][2] == 12, "the catalog's max_order_quantity for tea"
    assert any("clamped" in d["why"] for d in plan.dropped)


def test_the_total_is_recomputed_from_merchant_prices(seeded: Session) -> None:
    proposed = planning.PlannedCart(lines=[planning.PlannedLine(sku="DWP-TEA-001", quantity=2)])

    plan = planning.validate(seeded, proposed, planner_name="test")

    assert plan.estimated_total_minor == 90_000, "2 x 45000 paise, from the catalog"


def test_the_mandate_cap_can_never_be_below_the_cart_it_authorises(seeded: Session) -> None:
    proposed = planning.PlannedCart(
        lines=[planning.PlannedLine(sku="DWP-HDP-007", quantity=1)], budget_cap_minor=100
    )

    plan = planning.validate(seeded, proposed, planner_name="test")

    assert plan.budget_cap_minor >= plan.estimated_total_minor


def test_the_deterministic_planner_reads_a_budget_out_of_the_sentence(seeded: Session) -> None:
    document = planning.catalog_for_planning(seeded)
    proposed = planning.RuleBasedPlanner().propose("buy me tea under 1500 rupees", document)

    assert proposed.budget_cap_minor == 150_000


def test_the_deterministic_planner_carries_prose_constraints_through(seeded: Session) -> None:
    document = planning.catalog_for_planning(seeded)
    proposed = planning.RuleBasedPlanner().propose(
        "buy me some groceries, nothing perishable", document
    )

    assert "nothing perishable" in proposed.natural_language


def test_the_planner_is_deterministic_in_tests(seeded: Session) -> None:
    """CI must never reach a model, and the same prompt must give the same cart every run."""
    chosen = planning.get_planner()
    assert chosen.name == "rule-based"

    document = planning.catalog_for_planning(seeded)
    first = chosen.propose("buy two packets of tea", document)
    second = chosen.propose("buy two packets of tea", document)
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- a console run


def test_a_console_run_completes_and_logs_every_step(seeded: Session) -> None:
    request = runner.RunRequest(prompt="buy two packets of tea")
    run = runner.create_run(seeded, request)
    seeded.commit()

    runner.start(run.id, run.agent_id, request, block=True)
    seeded.expire_all()

    settled = seeded.get(type(run), run.id)
    assert settled is not None
    assert settled.status == BuyerRunStatus.COMPLETED.value, settled.reason_code
    assert settled.evidence_packet_id, "every run files evidence, whatever the outcome"

    steps = [e.step for e in runner.events_for(seeded, run.id)]
    for expected in ("identity", "catalog", "plan", "open_mandates", "quote", "closed_mandates", "verdict"):
        assert expected in steps, f"the agent log is missing its {expected} step"


def test_a_console_run_that_buys_nothing_is_refused_not_crashed(seeded: Session) -> None:
    request = runner.RunRequest(prompt="buy me a submarine", budget_cap_minor=1)
    run = runner.create_run(seeded, request)
    seeded.commit()

    runner.start(run.id, run.agent_id, request, block=True)
    seeded.expire_all()

    settled = seeded.get(type(run), run.id)
    assert settled is not None
    assert settled.status in (BuyerRunStatus.REFUSED.value, BuyerRunStatus.COMPLETED.value)
    assert settled.status != BuyerRunStatus.ERROR.value


def test_a_console_run_with_a_prose_constraint_reaches_the_human(seeded: Session) -> None:
    """The kernel cannot settle "nothing perishable", so the run must escalate, not complete."""
    request = runner.RunRequest(
        prompt="buy some fresh paneer", natural_language=["nothing perishable"]
    )
    run = runner.create_run(seeded, request)
    seeded.commit()

    runner.start(run.id, run.agent_id, request, block=True)
    seeded.expire_all()

    settled = seeded.get(type(run), run.id)
    assert settled is not None
    assert settled.status in (
        BuyerRunStatus.ESCALATED.value,
        BuyerRunStatus.REFUSED.value,
    ), f"a prose constraint must never complete silently, got {settled.status}"


# --------------------------------------------------------------------------- Razorpay handler


def test_the_razorpay_handler_signature_is_checked_over_the_documented_bytes() -> None:
    secret = "ci-secret"
    order, payment = "order_TT6Kw1Rn1YtU43", "pay_TT6Q2UcBMTAePY"
    good = hmac.new(secret.encode(), f"{order}|{payment}".encode(), hashlib.sha256).hexdigest()

    assert verify_checkout_signature(order, payment, good, secret)
    assert not verify_checkout_signature(order, payment, good[:-1] + "0", secret)
    assert not verify_checkout_signature(order, "pay_somebody_elses", good, secret)
    assert not verify_checkout_signature("order_other", payment, good, secret)


@pytest.mark.parametrize("signature", [None, "", "   "])
def test_an_unsigned_handler_result_is_refused(signature: str | None) -> None:
    assert not verify_checkout_signature("order_x", "pay_y", signature, "ci-secret")


def test_the_handler_signature_is_not_the_webhook_signature() -> None:
    """Two different signatures over two different byte strings. Confusing them would be a hole."""
    from app.payments.gateway import verify_webhook_signature

    secret = "ci-secret"
    order, payment = "order_x", "pay_y"
    handler = hmac.new(
        secret.encode(), f"{order}|{payment}".encode(), hashlib.sha256
    ).hexdigest()

    assert not verify_webhook_signature(b'{"event":"payment.captured"}', handler, secret)


def test_a_refusal_for_an_unregistered_agent_tells_nobody(db: Session) -> None:
    """An attacker's forged credential must not ring the merchant's own phone.

    Refusals are for the person who registered the agent. Falling back to the configured principal
    on every refusal would mean a corpus run or a hostile scan sends hundreds of messages nobody
    can act on, which is how a useful notification becomes an alarm that gets muted.
    """
    transport = RecordingTransport()

    row = _notify(db, transport, "refused", reason_code="CRED_SIGNATURE_INVALID")

    assert row is not None and row.status == NotificationStatus.SKIPPED.value
    assert transport.sent == []
    assert "registered" in (row.error or "")


def test_money_that_moved_still_reaches_the_configured_principal(db: Session) -> None:
    """A completed purchase is always worth telling somebody about, connection or not."""
    transport = RecordingTransport()

    row = _notify(db, transport, "completed")

    assert row is not None and row.status == NotificationStatus.SENT.value
    assert transport.sent, "a completed purchase falls back to ESCALATION_HUMAN_WHATSAPP"


def test_a_model_outage_degrades_the_run_instead_of_killing_it(seeded: Session, monkeypatch) -> None:
    """A rate-limited or unreachable planner must cost a less clever cart, not the purchase.

    Found the hard way: Gemini's free tier is twenty requests a day, and the twenty-first run
    raised straight out of the planner and ended as an error with no cart, no verdict and no
    evidence.
    """

    class Exhausted:
        name = "gemini"

        def propose(self, prompt: str, catalog_document: list[object]) -> object:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(planning, "get_planner", lambda: Exhausted())

    request = runner.RunRequest(prompt="buy two packets of tea")
    run = runner.create_run(seeded, request)
    seeded.commit()

    runner.start(run.id, run.agent_id, request, block=True)
    seeded.expire_all()

    settled = seeded.get(type(run), run.id)
    assert settled is not None
    assert settled.status != BuyerRunStatus.ERROR.value, "a model outage must not end the run"
    assert settled.planner == "rule-based", "it must fall back to the deterministic planner"

    steps = [e.step for e in runner.events_for(seeded, run.id)]
    assert "planner_fallback" in steps, "the log must say the model was unavailable"


def test_a_stated_budget_is_a_cap_and_not_a_suggestion(seeded):
    """The console's budget control has to bind, or it is decoration.

    Against the previous code this failed: validate() raised the cap to whatever the cart cost,
    so a buyer who said 2000 got a mandate for the full 8000.
    """
    expensive = catalog_service.by_sku(seeded, "DWP-KBD-008")
    assert expensive is not None
    cap = expensive.product.price_minor // 2

    proposed = planning.PlannedCart(
        lines=[planning.PlannedLine(sku="DWP-KBD-008", quantity=1)],
        budget_cap_minor=planning.DEFAULT_BUDGET_MINOR,
        natural_language=[],
        rationale="one keyboard",
    )
    plan = planning.validate(seeded, proposed, planner_name="test", hard_cap_minor=cap)

    assert plan.budget_cap_minor == cap, "the buyer's stated cap was widened"
    assert plan.estimated_total_minor <= cap, "the cart was allowed past the cap"
    assert not plan.lines, "the unaffordable line should have been dropped"
    assert any("budget" in d["why"] for d in plan.dropped), "the drop was not explained"


def test_without_a_stated_cap_the_mandate_still_covers_its_cart(seeded):
    """A mandate that cannot pay for the cart it authorises is useless, so this path is unchanged."""
    proposed = planning.PlannedCart(
        lines=[planning.PlannedLine(sku="DWP-KBD-008", quantity=1)],
        budget_cap_minor=1,
        natural_language=[],
        rationale="one keyboard",
    )
    plan = planning.validate(seeded, proposed, planner_name="test")
    assert plan.lines
    assert plan.budget_cap_minor >= plan.estimated_total_minor


def test_two_connections_never_share_a_derived_agent_id(seeded):
    """Receipts route by agent id, so a shared one sends somebody else's purchase to a stranger."""
    first = connections.create_connection(seeded, label="Shopping agent", whatsapp="+919000000001")
    second = connections.create_connection(seeded, label="Shopping agent", whatsapp="+919000000002")

    assert first.connection.agent_id != second.connection.agent_id


def test_a_receipt_is_not_routed_when_two_connections_claim_one_agent_id(seeded):
    """An explicitly supplied duplicate must resolve to nobody rather than to a guess."""
    from app.notify import service as notify
    from app.settings import settings

    connections.create_connection(
        seeded, label="A", agent_id="agent:collide", whatsapp="+919000000003"
    )
    connections.create_connection(
        seeded, label="B", agent_id="agent:collide", whatsapp="+919000000004"
    )
    seeded.flush()

    target = notify.recipient_for(
        seeded,
        agent_id="agent:collide",
        connection_id=None,
        kind=NotificationKind.PURCHASE_COMPLETED,
    )
    assert target is None or target.number == settings.ESCALATION_HUMAN_WHATSAPP
