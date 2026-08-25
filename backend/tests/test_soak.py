"""Long-running tests, deselected by default.

    pytest -m soak                 the full sizes
    SOAK_SCALE=ci pytest -m soak   the bounded sizes CI runs

These are the same guarantees the fast suite checks, driven hard enough and long enough that
anything which only fails at volume has a chance to. A lock that is nearly right, a counter that
drifts by one every few hundred writes and a chain that forks under contention all pass a
three-thread test.

Nothing here reaches Meta, Gemini or Razorpay: conftest forces APP_ENV=testing, which selects the
recording transport and the stub gateway.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.checkout.complete import complete
from app.checkout.quote import RequestedLine, create_quote
from app.db import base as db_base
from app.db.base import utcnow
from app.db.models import (
    BudgetReservation,
    EvidencePacket,
    OpenMandate,
    Product,
    ReservationStatus,
)
from app.disputes import responder
from app.escalation.whatsapp import RecordingTransport
from app.evidence import locker
from app.harness import factory
from app.kernel import budget, inventory
from app.payments.gateway import StubGateway
from app.semantic.client import KeywordSemanticClient
from app.settings import settings

pytestmark = pytest.mark.soak

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# CI runs the same cases at a size that finishes in a couple of minutes. Locally the default is
# the size worth quoting in the README.
_FULL = os.environ.get("SOAK_SCALE", "full") != "ci"


def scaled(full: int, ci: int) -> int:
    return full if _FULL else ci


SKU, TITLE = "DWP-NTB-011", "Hardcover Notebook A5"
UNIT_MINOR = 65_000


def _mandate(session, cap_minor: int) -> str:
    row = OpenMandate(
        kind="checkout",
        digest=f"soak-{utcnow().timestamp()}-{cap_minor}",
        sd_jwt="",
        claims={},
        agent_id="agent:soak",
        key_thumbprint="tp",
        issuer_id="did:web:trusted-surface.dwarpal.test",
        tier="accredited",
        cap_minor=cap_minor,
        currency="INR",
    )
    session.add(row)
    session.commit()
    return row.id


def test_a_cap_holds_against_hundreds_of_simultaneous_draws(db) -> None:
    """The headline concurrency claim, at a size where a nearly-correct lock gives up."""
    draws = scaled(500, 60)
    grant_limit = draws // 10
    cap = UNIT_MINOR * grant_limit
    mandate_id = _mandate(db, cap)

    def draw(_index: int) -> bool:
        session = db_base.SessionFactory()
        try:
            # Load first, the way a checkout does. A locked read that returns this cached copy is
            # the bug this size is here to catch.
            session.get(OpenMandate, mandate_id)
            reservation = budget.reserve(session, mandate_id, UNIT_MINOR, "soak")
            budget.commit(session, reservation.id)
            session.commit()
            return True
        except budget.BudgetExceeded:
            session.rollback()
            return False
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=32) as pool:
        granted = sum(pool.map(draw, range(draws)))

    settled = db.get(OpenMandate, mandate_id, populate_existing=True)
    assert settled is not None
    assert settled.committed_minor <= cap, (
        f"cap breached under {draws} draws: {settled.committed_minor} > {cap}"
    )
    assert granted == grant_limit, f"expected {grant_limit} of {draws} to be granted, got {granted}"


def test_the_ledger_still_adds_up_after_thousands_of_reservations(db) -> None:
    """Reservations, commits and releases must never lose or invent money."""
    rounds = scaled(400, 60)
    cap = UNIT_MINOR * rounds
    mandate_id = _mandate(db, cap)

    committed = 0
    for index in range(rounds):
        reservation = budget.reserve(db, mandate_id, UNIT_MINOR, f"soak-{index}")
        if index % 3 == 0:
            budget.release(db, reservation.id)
        else:
            budget.commit(db, reservation.id)
            committed += UNIT_MINOR
    db.commit()

    settled = db.get(OpenMandate, mandate_id, populate_existing=True)
    assert settled is not None
    assert settled.committed_minor == committed
    still_reserved = int(
        db.scalar(
            select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                BudgetReservation.mandate_id == mandate_id,
                BudgetReservation.status == ReservationStatus.RESERVED,
            )
        )
        or 0
    )
    assert still_reserved == 0, "every reservation was settled, so none may still be held"


def test_one_agent_cannot_exhaust_a_shelf_however_long_it_tries(seeded) -> None:
    """The hold quota has to hold for as long as an agent keeps asking, not just at first."""
    attempts = scaled(200, 40)
    product = seeded.scalar(select(Product).where(Product.sku == SKU))
    assert product is not None
    before = inventory.available(seeded, product)

    refused = 0
    for index in range(attempts):
        try:
            inventory.place_hold(
                seeded,
                sku=SKU,
                quantity=1,
                agent_id="agent:soak-hog",
                checkout_id=f"co_soak_{index}",
                correlation_id=f"soak-{index}",
            )
        except inventory.HoldQuotaExceeded:
            refused += 1
        seeded.flush()

    assert refused > 0, "the quota never engaged, so it is not a quota"
    held = inventory.agent_active_holds(seeded, "agent:soak-hog")
    assert held <= settings.INVENTORY_HOLD_QUOTA_PER_AGENT
    after = inventory.available(seeded, product)
    assert before - after <= settings.INVENTORY_HOLD_QUOTA_PER_AGENT, (
        "one agent removed more stock from sale than its quota permits"
    )


def test_the_chain_holds_over_a_long_run_and_verifies_offline(db, tmp_path: Path) -> None:
    """A large chain, exported and checked by the standalone tool with nothing else running.

    The verifier imports nothing from the application. If it needed the running merchant to agree
    with it, it would prove nothing about what a third party could check.
    """
    packets = scaled(1000, 120)
    for index in range(packets):
        locker.append(db, correlation_id=f"soak_{index // 7}", body={"outcome": "soak", "n": index})
    db.commit()

    total = int(db.scalar(select(func.count(EvidencePacket.seq))) or 0)
    assert total >= packets

    report = locker.verify_chain(db)
    assert report["valid"], report["problems"]

    rows = locker.export_rows(db)
    jsonl = tmp_path / "evidence.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    from app.keys import merchant_jwks

    jwks = tmp_path / "merchant_jwks.json"
    jwks.write_text(json.dumps(merchant_jwks(), indent=2), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND_ROOT / "tools" / "verify_evidence.py"),
            "--jsonl",
            str(jsonl),
            "--jwks",
            str(jwks),
            "--min-packets",
            str(packets),
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_ROOT),
        check=False,
    )
    assert result.returncode == 0, f"offline verification failed:\n{result.stdout}\n{result.stderr}"


def test_concurrent_appends_never_fork_the_chain(db) -> None:
    """Two writers choosing the same predecessor is the chain forking, not a retryable blip."""
    writers = scaled(200, 40)

    def append(index: int) -> bool:
        session = db_base.SessionFactory()
        try:
            locker.append(session, correlation_id=f"fork_{index}", body={"n": index})
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        written = sum(pool.map(append, range(writers)))

    assert written == writers, f"{writers - written} appends were lost under contention"
    report = locker.verify_chain(db)
    assert report["valid"], report["problems"]


def test_a_batch_of_purchases_and_attacks_settles_correctly(seeded) -> None:
    """The whole gate, end to end, repeated. Every attack refused and every honest cart allowed."""
    honest_rounds = scaled(60, 12)
    gateway = StubGateway()
    allowed = 0
    refused_honest: list[str] = []

    for index in range(honest_rounds):
        principals = factory.Principals.create(agent_id=f"agent:soak-honest-{index}")
        quoted = create_quote(
            seeded,
            agent_id=principals.agent_id,
            correlation_id=f"soak_honest_{index}",
            lines=[RequestedLine(sku=SKU, quantity=1)],
        )
        presentation = factory.present(
            principals,
            factory.spec_for_cart([(SKU, TITLE, 1)]),
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"soak-honest-{index}",
        )
        outcome = complete(
            seeded,
            presentation.credentials,
            correlation_id=f"soak_honest_{index}",
            gateway=gateway,
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
        )
        if outcome.status == "completed":
            allowed += 1
        else:
            refused_honest.append(outcome.reason_code.value)
        seeded.flush()

    assert not refused_honest, f"honest carts were refused: {sorted(set(refused_honest))}"
    assert allowed == honest_rounds

    tampers = [
        factory.Tamper(forge_issuer_signature=True),
        factory.Tamper(wrong_agent_key=True),
        factory.Tamper(expired=True),
        factory.Tamper(unknown_issuer=True),
        factory.Tamper(drop_key_binding=True),
        factory.Tamper(payment_amount_minor=1),
    ]
    missed: list[str] = []
    for index in range(scaled(60, 12)):
        tamper = tampers[index % len(tampers)]
        principals = factory.Principals.create(agent_id=f"agent:soak-attack-{index}")
        quoted = create_quote(
            seeded,
            agent_id=principals.agent_id,
            correlation_id=f"soak_attack_{index}",
            lines=[RequestedLine(sku=SKU, quantity=1)],
        )
        presentation = factory.present(
            principals,
            factory.spec_for_cart([(SKU, TITLE, 1)]),
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"soak-attack-{index}",
            tamper=tamper,
        )
        outcome = complete(
            seeded,
            presentation.credentials,
            correlation_id=f"soak_attack_{index}",
            gateway=gateway,
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
        )
        if outcome.status == "completed":
            missed.append(str(tamper))
        seeded.flush()

    assert not missed, f"attacks that completed anyway: {missed}"


def test_a_batch_of_disputes_is_scored_and_not_all_contested(seeded) -> None:
    """A responder that recommends contesting everything is worthless, at any batch size."""
    cases = scaled(120, 20)
    gateway = StubGateway()
    correlations: list[str] = []

    for index in range(cases // 2):
        principals = factory.Principals.create(agent_id=f"agent:soak-dispute-{index}")
        quoted = create_quote(
            seeded,
            agent_id=principals.agent_id,
            correlation_id=f"soak_dispute_{index}",
            lines=[RequestedLine(sku=SKU, quantity=1)],
        )
        presentation = factory.present(
            principals,
            factory.spec_for_cart([(SKU, TITLE, 1)]),
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            nonce=f"soak-dispute-{index}",
        )
        complete(
            seeded,
            presentation.credentials,
            correlation_id=f"soak_dispute_{index}",
            gateway=gateway,
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
        )
        correlations.append(f"soak_dispute_{index}")
        seeded.flush()

    evidenced = [
        responder.respond(seeded, correlation_id=c, claim="not authorised") for c in correlations
    ]
    baseline = [
        responder.respond(seeded, correlation_id=f"never_happened_{i}", claim="not authorised")
        for i in range(cases // 2)
    ]
    seeded.flush()

    contested = sum(1 for d in evidenced if d.recommendation == "contest")
    baseline_contested = sum(1 for d in baseline if d.recommendation == "contest")

    assert contested > 0, "evidence that never supports a defence is not evidence"
    assert baseline_contested == 0, (
        "a transaction with nothing behind it was recommended for contest, which is the "
        "recommendation that loses money"
    )
    assert all(d.strength_score is not None for d in evidenced + baseline)


def test_reservations_left_behind_by_abandoned_carts_all_expire(db) -> None:
    """Abandoned attempts must free their budget, however many of them there are."""
    rounds = scaled(300, 50)
    cap = UNIT_MINOR * rounds
    mandate_id = _mandate(db, cap)

    for index in range(rounds):
        budget.reserve(db, mandate_id, UNIT_MINOR, f"abandoned-{index}", ttl_seconds=-1)
    db.commit()

    # reserve() expires whatever is already stale on the same mandate before it decides, so most
    # of these are already gone by the time the sweep runs. That is the point: an abandoned cart
    # frees its budget at the next attempt, without waiting for anything to be swept.
    swept = budget.sweep_expired(db)
    db.commit()
    assert swept >= 1

    state = budget.state(db, mandate_id)
    assert state.reserved_minor == 0, "abandoned reservations are still holding budget"
    assert state.available_minor == cap, "the whole cap is available again"


def test_inventory_holds_expire_in_bulk(seeded) -> None:
    product = seeded.scalar(select(Product).where(Product.sku == SKU))
    assert product is not None
    before = inventory.available(seeded, product)
    rounds = scaled(100, 20)

    for index in range(rounds):
        hold = inventory.place_hold(
            seeded,
            sku=SKU,
            quantity=1,
            agent_id=f"agent:soak-expiry-{index}",
            checkout_id=f"co_expiry_{index}",
            correlation_id=f"expiry-{index}",
        )
        hold.expires_at = utcnow() - timedelta(seconds=1)
    seeded.flush()

    inventory.expire_stale(seeded)
    seeded.flush()
    assert inventory.available(seeded, product) == before, (
        "expired holds did not return their stock to sale"
    )
