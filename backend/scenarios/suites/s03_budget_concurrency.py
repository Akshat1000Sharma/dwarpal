"""s03 - many sessions drawing on one budget at the same time.

The interesting failure is not one agent overspending. It is two sessions each reading the same
remaining budget, each deciding there is room, and each committing. Against an application-level
read-then-write both succeed and the cap is breached; against a row lock exactly one does.

The race is set up in two phases on purpose. Carts are quoted one at a time, because the per-agent
hold quota deliberately caps how many carts one agent may hold at once and racing the quote would
be testing that limit instead. Then every completion is fired at the same moment, which is the
actual contention: several valid, held, priced carts all trying to commit against one cap.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.settings import settings
from scenarios.harness import RUN_ID, Context, Shopper, Suite, pay_order, record

# One notebook, 650.00. A cap of two of them means every draw after the second must lose.
SKU, TITLE, UNIT_MINOR = "DWP-NTB-011", "Hardcover Notebook A5", 65_000


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s03",
        "Budget under concurrency",
        "Concurrent draws against one mandate cap. The cap must hold under contention.",
    )
    client = ctx.client
    cart = [(SKU, TITLE, 1)]
    # Bounded by the merchant's own hold quota: one agent cannot hold more carts than that, and
    # pretending otherwise would test the quota rather than the budget lock.
    draws = max(2, min(ctx.scale.concurrency, settings.INVENTORY_HOLD_QUOTA_PER_AGENT))
    # Declared up front so a case that raises cannot make a later one fail for the wrong reason.
    settled_count = 0

    with suite.case(
        "cap_holds_under_contention",
        proves="concurrent authorisations cannot all succeed if together they exceed the cap",
        expected=f"at most 2 of {draws} simultaneous commits settle against a 2-unit cap",
    ) as case:
        shopper = Shopper(client, "s03-contended")
        cap = UNIT_MINOR * 2
        issued = shopper.authorise(cart, amount_cap_minor=cap, budget_minor=cap)

        quotes = [shopper.quoted([(SKU, 1)]) for _ in range(draws)]
        presentations = [
            shopper.present(
                issued, quote, audience=ctx.audience, nonce=f"{shopper.agent_id}-draw-{index}"
            )
            for index, quote in enumerate(quotes)
        ]

        def commit(presentation: object) -> dict[str, object]:
            _status, outcome = shopper.complete(presentation)  # type: ignore[arg-type]
            if outcome.get("status") == "awaiting_payment":
                pay_order(client, outcome)
            return outcome

        with ThreadPoolExecutor(max_workers=draws) as pool:
            outcomes = list(pool.map(commit, presentations))

        settled = [o for o in outcomes if o.get("status") in ("completed", "awaiting_payment")]
        settled_count = len(settled)
        codes = sorted({str(o.get("reason_code")) for o in outcomes})
        record(
            case,
            settled_count <= 2,
            f"{settled_count} of {len(outcomes)} simultaneous commits settled; codes seen {codes}",
        )

    with suite.case(
        "refusals_are_budget_refusals",
        proves="the losers are told why, in a code they can act on, not with a server error",
        expected="every loser carries a budget-family code and none answers 5xx",
    ) as case:
        shopper = Shopper(client, "s03-losers")
        cap = UNIT_MINOR
        issued = shopper.authorise(cart, amount_cap_minor=cap, budget_minor=cap)

        results = []
        for index in range(3):
            quote = shopper.quoted([(SKU, 1)])
            presentation = shopper.present(
                issued, quote, audience=ctx.audience, nonce=f"{shopper.agent_id}-seq-{index}"
            )
            status, outcome = shopper.complete(presentation)
            if outcome.get("status") == "awaiting_payment":
                pay_order(client, outcome)
            results.append((status, str(outcome.get("reason_code"))))

        refusals = [(s, c) for s, c in results if not c.startswith("APPROVED")]
        clean = all(
            s < 500 and any(k in c for k in ("BUDGET", "RECURRENCE", "AMOUNT", "VELOCITY"))
            for s, c in refusals
        )
        record(case, clean and bool(refusals), f"{results}")

    with suite.case(
        "the_cap_is_never_breached_in_the_ledger",
        proves="what the merchant recorded agrees with what it allowed",
        expected="committed spend on a mandate never exceeds its cap",
    ) as case:
        status, mandates = client.get("/merchant/mandates")
        # Scoped to this run. The log is cumulative across every run against this database, and a
        # mandate a previous build breached stays breached forever; reporting it here would say
        # the current code is wrong when it is not.
        rows = [m for m in mandates.get("mandates", []) if RUN_ID in m["agent_id"]]
        breached = [
            m for m in rows if (m["cap"]["amount"] or 0) > 0
            and m["committed"]["amount"] > m["cap"]["amount"]
        ]
        record(
            case,
            status == 200 and not breached,
            f"{len(rows)} mandates inspected, {len(breached)} breached",
            note="; ".join(
                f"{m['id']}: {m['committed']['amount']} > {m['cap']['amount']}"
                for m in breached[:3]
            ),
        )

    with suite.case(
        "reservations_are_accounted_for_separately",
        proves="an abandoned cart holds budget temporarily, and the books say so",
        expected="remaining equals cap minus committed minus reserved, on every mandate",
    ) as case:
        _status, mandates = client.get("/merchant/mandates")
        rows = [
            m
            for m in mandates.get("mandates", [])
            if m["cap"]["amount"] and RUN_ID in m["agent_id"]
        ]
        inconsistent = [
            m
            for m in rows
            if m["remaining"]["amount"]
            != max(0, m["cap"]["amount"] - m["committed"]["amount"] - m["reserved"]["amount"])
        ]
        record(
            case,
            bool(rows) and not inconsistent,
            f"{len(rows)} capped mandates, {len(inconsistent)} whose arithmetic disagrees",
        )

    with suite.case(
        "one_agents_contention_does_not_stop_another",
        proves="a mandate hitting its cap is that mandate's problem, not the merchant's",
        expected="a fresh shopper still completes while the contended one is exhausted",
    ) as case:
        fresh = Shopper(client, "s03-unaffected")
        outcome = fresh.buy(cart, audience=ctx.audience)
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"{outcome.get('status')} after {settled_count} contended commits settled",
        )

    return suite
