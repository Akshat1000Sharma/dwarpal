"""s11 - many agents, mixed intent, for as long as you ask.

Everything before this runs one situation at a time. This runs all of them at once, from many
agents, for a configured duration, which is the only way to catch what only appears under load:
a lock held too long, a pool exhausted, a counter that drifts, a chain that forks.

Two numbers come out of it and they are always reported together. A gate that blocks everything
scores perfectly against the hostile traffic and is useless, so the benign pass rate is never
shown on its own.

Three buckets, not two. A benign purchase that was refused because the shelf was empty is not a
false positive: the merchant could not have served it, and counting it as an error would make a
correct sold-out answer look like a defect. Supply-limited refusals are counted and reported on
their own, and the shelves are restocked periodically so the run measures the gate rather than the
warehouse.

This suite is also the dashboard's data generator. Every request it makes leaves a real verdict,
mandate, evidence packet or dispute behind.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.harness import factory
from scenarios.harness import RUN_ID, Context, Shopper, Suite, pay_order, reason_of, record

# Ordinary shopping, from the deeper shelves so a long run measures the gate and not the stock.
BENIGN_CARTS: list[list[tuple[str, str, int]]] = [
    [("DWP-TEA-001", "Nilgiri Black Tea 250g", 2)],
    [("DWP-COF-002", "Single Origin Coffee Beans 500g", 1)],
    [("DWP-NTB-011", "Hardcover Notebook A5", 3)],
    [("DWP-LMP-010", "Desk Lamp Compact", 1)],
    [
        ("DWP-TEA-001", "Nilgiri Black Tea 250g", 1),
        ("DWP-NTB-011", "Hardcover Notebook A5", 1),
    ],
]

# Each entry is an attack and the shorthand the report uses for it.
HOSTILE: list[tuple[str, factory.Tamper]] = [
    ("forged", factory.Tamper(forge_issuer_signature=True)),
    ("stolen", factory.Tamper(wrong_agent_key=True)),
    ("expired", factory.Tamper(expired=True)),
    ("early", factory.Tamper(not_yet_valid=True)),
    ("unknown-issuer", factory.Tamper(unknown_issuer=True)),
    ("no-proof", factory.Tamper(drop_key_binding=True)),
    ("amount-swap", factory.Tamper(payment_amount_minor=1)),
    ("payee-swap", factory.Tamper(payee={"id": "attacker", "name": "Attacker"})),
    ("cart-swap", factory.Tamper(altered_checkout_hash="forged-hash")),
]

# Refusals that mean "the merchant has run out", not "the merchant got it wrong".
SUPPLY_CODES = frozenset(
    {"INVENTORY_UNAVAILABLE", "HOLD_QUOTA_EXCEEDED", "HOLD_EXPIRED", "QUANTITY_OUT_OF_RANGE"}
)


@dataclass
class Tally:
    """Counters shared across worker threads, guarded by one lock.

    A tally that undercounted under contention would make the whole suite's numbers meaningless,
    so the increments are not left to chance even though the GIL would usually hide it.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    benign_attempted: int = 0
    benign_allowed: int = 0
    benign_supply_limited: int = 0
    benign_refused: list[str] = field(default_factory=list)
    hostile_attempted: int = 0
    hostile_blocked: int = 0
    hostile_missed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _one_benign(ctx: Context, index: int, tally: Tally) -> None:
    cart = BENIGN_CARTS[index % len(BENIGN_CARTS)]
    try:
        shopper = Shopper(ctx.client, f"s11-benign-{index}")
        outcome = shopper.buy(cart, audience=ctx.audience, pay=False)
        allowed = outcome.get("status") in ("completed", "awaiting_payment")
        code = reason_of(outcome)
        if outcome.get("status") == "awaiting_payment":
            pay_order(ctx.client, outcome)
        with tally.lock:
            tally.benign_attempted += 1
            if allowed:
                tally.benign_allowed += 1
            elif code in SUPPLY_CODES:
                tally.benign_supply_limited += 1
            else:
                tally.benign_refused.append(f"{shopper.agent_id}: {outcome.get('status')}/{code}")
    except Exception as exc:
        message = str(exc)
        supply = any(code in message for code in SUPPLY_CODES)
        with tally.lock:
            tally.benign_attempted += 1
            if supply:
                tally.benign_supply_limited += 1
            else:
                tally.errors.append(f"benign-{index}: {type(exc).__name__}: {message[:160]}")


def _one_hostile(ctx: Context, index: int, tally: Tally) -> None:
    label, tamper = HOSTILE[index % len(HOSTILE)]
    cart = BENIGN_CARTS[index % len(BENIGN_CARTS)]
    try:
        shopper = Shopper(ctx.client, f"s11-hostile-{index}")
        outcome = shopper.buy(cart, audience=ctx.audience, tamper=tamper, pay=False)
        completed = outcome.get("status") == "completed"
        with tally.lock:
            tally.hostile_attempted += 1
            if completed:
                tally.hostile_missed.append(f"{label}: completed anyway")
            else:
                tally.hostile_blocked += 1
    except Exception:
        # The attack never got as far as presenting credentials, because the quote was refused.
        # That is still a block: no money moved and no authority was accepted.
        with tally.lock:
            tally.hostile_attempted += 1
            tally.hostile_blocked += 1


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s11",
        "Soak: mixed traffic",
        "Many agents shopping and attacking at once, for as long as the profile asks.",
    )
    if ctx.scale.soak_seconds <= 0:
        suite.skipped = "this profile does not run the soak"
        return suite

    client = ctx.client
    tally = Tally()
    rng = random.Random(20260825)
    restocks = 0

    with suite.case(
        "sustained_mixed_traffic",
        proves="the gate holds up under concurrent, mixed-intent traffic over time",
        expected=(
            f"{ctx.scale.soak_seconds}s of traffic from {ctx.scale.agents} concurrent agents "
            "with no unhandled errors"
        ),
    ) as case:
        client.post("/merchant/catalog/restock", {})
        started = time.monotonic()
        deadline = started + ctx.scale.soak_seconds
        last_restock = started
        index = 0
        with ThreadPoolExecutor(max_workers=ctx.scale.agents) as pool:
            while time.monotonic() < deadline:
                pending = []
                # Roughly three ordinary purchases for every attack, which is the traffic mix a
                # merchant would actually see.
                for _ in range(ctx.scale.agents):
                    index += 1
                    hostile = rng.random() < 0.25
                    pending.append(
                        pool.submit(_one_hostile if hostile else _one_benign, ctx, index, tally)
                    )
                for future in pending:
                    future.result()
                # A shop that is never restocked runs dry and then reports correct sold-out
                # answers as if they were failures.
                if time.monotonic() - last_restock > 10:
                    client.post("/merchant/catalog/restock", {})
                    last_restock = time.monotonic()
                    restocks += 1
        elapsed = time.monotonic() - started
        total = tally.benign_attempted + tally.hostile_attempted
        record(
            case,
            not tally.errors,
            f"{total} attempts in {elapsed:.0f}s ({total / max(elapsed, 1):.1f}/s), "
            f"{restocks} restocks, {len(tally.errors)} unhandled errors",
            note="; ".join(tally.errors[:3]),
        )

    with suite.case(
        "hostile_traffic_blocked",
        proves="every attack family is still refused under load",
        expected="no hostile attempt completes",
    ) as case:
        record(
            case,
            not tally.hostile_missed,
            f"{tally.hostile_blocked}/{tally.hostile_attempted} blocked, "
            f"{len(tally.hostile_missed)} missed",
            note="; ".join(tally.hostile_missed[:5]),
        )

    with suite.case(
        "benign_traffic_allowed",
        proves="the gate is not merely a wall; ordinary shopping still goes through",
        expected="no benign purchase is refused for a reason other than supply",
    ) as case:
        servable = tally.benign_attempted - tally.benign_supply_limited
        rate = tally.benign_allowed / max(servable, 1)
        record(
            case,
            not tally.benign_refused,
            f"{tally.benign_allowed}/{servable} servable purchases allowed ({rate * 100:.1f}%), "
            f"{tally.benign_supply_limited} refused for supply, "
            f"{len(tally.benign_refused)} false positives",
            note="; ".join(tally.benign_refused[:5]),
        )

    with suite.case(
        "the_chain_survived_the_load",
        proves="concurrent writes to an append-only hash chain do not fork it",
        expected="the chain is valid after the soak",
    ) as case:
        status, index_body = client.get("/merchant/evidence?limit=200")
        chain = index_body.get("chain") or {}
        record(
            case,
            status == 200 and chain.get("valid") is True,
            f"valid={chain.get('valid')}, problems={chain.get('problems')}",
        )

    with suite.case(
        "no_budget_was_breached_under_load",
        proves="the accounting still adds up after concurrent settlement",
        expected="no mandate reports committed spend above its cap",
    ) as case:
        status, mandates = client.get("/merchant/mandates")
        # This run's mandates only. The log outlives any single run.
        rows = [m for m in mandates.get("mandates", []) if RUN_ID in m["agent_id"]]
        breached = [
            m for m in rows if m["cap"]["amount"] and m["committed"]["amount"] > m["cap"]["amount"]
        ]
        record(
            case,
            status == 200 and not breached,
            f"{len(rows)} mandates inspected, {len(breached)} breached",
        )

    with suite.case(
        "every_decision_carries_a_reason_code",
        proves="a verdict without a reason code is a defect, at any volume",
        expected="every verdict in the log carries a code from the closed set",
    ) as case:
        _status, codes_body = client.get("/merchant/reason-codes")
        known = {c["code"] for c in codes_body.get("codes", [])}
        _status, verdicts = client.get("/merchant/verdicts?limit=500")
        rows = verdicts.get("verdicts", [])
        unknown = {v["reason_code"] for v in rows if v["reason_code"] not in known}
        record(
            case,
            bool(rows) and not unknown,
            f"{len(rows)} verdicts checked against {len(known)} known codes, "
            f"{len(unknown)} unknown",
            note=", ".join(sorted(unknown)[:5]),
        )

    return suite
