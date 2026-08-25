"""s05 - evading a cap by going around it.

A per-transaction limit that only looks at one transaction is not a limit. An agent that cannot
spend 10,000 at once can spend 1,000 ten times, and the human's instruction has been defeated
without a single rule being broken. The merchant has to hold a rolling aggregate.

The velocity cases are the merchant's own controls rather than the human's: a merchant may want to
stop an agent it does not like without waiting for the principal to revoke anything.
"""

from __future__ import annotations

from scenarios.harness import Context, Shopper, Suite, pay_order, record

SKU, TITLE, UNIT_MINOR = "DWP-NTB-011", "Hardcover Notebook A5", 65_000


def _repeat(ctx: Context, shopper: Shopper, issued: object, attempts: int) -> list[str]:
    """Present the same standing authority against a fresh quote, repeatedly."""
    codes: list[str] = []
    for index in range(attempts):
        try:
            quote = shopper.quoted([(SKU, 1)])
        except Exception as exc:  # a quote refusal is itself an outcome worth recording
            codes.append(f"quote:{type(exc).__name__}")
            continue
        presentation = shopper.present(
            issued,  # type: ignore[arg-type]
            quote,
            audience=ctx.audience,
            nonce=f"{shopper.agent_id}-split-{index}",
        )
        _status, outcome = shopper.complete(presentation)
        if outcome.get("status") == "awaiting_payment":
            pay_order(ctx.client, outcome)
        codes.append(str(outcome.get("reason_code")))
    return codes


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s05",
        "Structuring and velocity",
        "Splitting a purchase to dodge a cap, and the merchant's own per-agent limits.",
    )
    client = ctx.client
    cart = [(SKU, TITLE, 1)]
    attempts = ctx.scale.structuring_attempts

    with suite.case(
        "splitting_does_not_defeat_the_budget",
        proves="a rolling aggregate stops what the individual transactions were designed to dodge",
        expected="the run stops before the aggregate exceeds a 3-unit budget",
    ) as case:
        shopper = Shopper(client, "s05-splitter")
        budget = UNIT_MINOR * 3
        issued = shopper.authorise(cart, amount_cap_minor=UNIT_MINOR, budget_minor=budget)
        codes = _repeat(ctx, shopper, issued, attempts)
        approved = [c for c in codes if c.startswith("APPROVED")]
        record(
            case,
            len(approved) <= 3,
            f"{len(approved)} of {attempts} approved; codes {codes}",
        )

    with suite.case(
        "the_refusal_names_the_aggregate",
        proves="the agent is told it hit a running limit, not merely that it was refused",
        expected="a BUDGET, STRUCTURING or VELOCITY code among the refusals",
    ) as case:
        shopper = Shopper(client, "s05-named")
        issued = shopper.authorise(
            cart, amount_cap_minor=UNIT_MINOR, budget_minor=UNIT_MINOR * 2
        )
        codes = _repeat(ctx, shopper, issued, attempts)
        refusals = [c for c in codes if not c.startswith("APPROVED")]
        named = any(
            any(k in c for k in ("BUDGET", "STRUCTURING", "VELOCITY", "RECURRENCE"))
            for c in refusals
        )
        record(case, named, f"refusals {sorted(set(refusals))}")

    with suite.case(
        "recurrence_is_exhausted_not_ignored",
        proves="a mandate that authorises N uses stops working at N+1",
        expected="CONSTRAINT_RECURRENCE_EXHAUSTED after two uses",
    ) as case:
        shopper = Shopper(client, "s05-recurrence")
        issued = shopper.authorise(cart, max_occurrences=2)
        codes = _repeat(ctx, shopper, issued, 4)
        record(
            case,
            "CONSTRAINT_RECURRENCE_EXHAUSTED" in codes,
            f"codes {codes}",
        )

    with suite.case(
        "merchant_can_cap_an_agents_spend_per_window",
        proves="the merchant has its own controls, independent of the human's authority",
        expected="VELOCITY_SPEND_EXCEEDED once the merchant lowers the limit",
    ) as case:
        shopper = Shopper(client, "s05-velocity")
        issued = shopper.authorise(cart, amount_cap_minor=1_000_000, budget_minor=1_000_000)
        _repeat(ctx, shopper, issued, 1)
        status, _ = client.patch(
            f"/merchant/agents/{shopper.agent_id}", {"max_spend_per_window_minor": 1}
        )
        codes = _repeat(ctx, shopper, issued, 2)
        record(
            case,
            status == 200 and "VELOCITY_SPEND_EXCEEDED" in codes,
            f"PATCH {status}, then codes {codes}",
        )

    with suite.case(
        "kill_switch_stops_one_agent_immediately",
        proves="a merchant can stop one agent without touching any other",
        expected="AGENT_KILL_SWITCH for the stopped agent",
    ) as case:
        shopper = Shopper(client, "s05-killed")
        issued = shopper.authorise(cart)
        _repeat(ctx, shopper, issued, 1)
        status, _ = client.patch(f"/merchant/agents/{shopper.agent_id}", {"kill_switch": True})
        codes = _repeat(ctx, shopper, issued, 1)
        record(
            case,
            status == 200 and codes == ["AGENT_KILL_SWITCH"],
            f"PATCH {status}, then codes {codes}",
        )
        ctx_killed = shopper.agent_id

    with suite.case(
        "the_kill_switch_is_not_a_shutdown",
        proves="stopping one agent must not stop the merchant",
        expected="a different agent still transacts",
    ) as case:
        other = Shopper(client, "s05-survivor")
        outcome = other.buy(cart, audience=ctx.audience)
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"{outcome.get('status')} while {ctx_killed} is stopped",
        )

    with suite.case(
        "a_forbidden_category_is_refused_for_that_agent_only",
        proves="category gates are per agent and take effect immediately",
        expected="CATEGORY_FORBIDDEN after blocking stationery for one agent",
    ) as case:
        blocked = Shopper(client, "s05-blocked")
        issued = blocked.authorise(cart)
        _repeat(ctx, blocked, issued, 1)
        status, _ = client.patch(
            f"/merchant/agents/{blocked.agent_id}", {"blocked_categories": ["stationery"]}
        )
        codes = _repeat(ctx, blocked, issued, 1)
        record(
            case,
            status == 200 and codes == ["CATEGORY_FORBIDDEN"],
            f"PATCH {status}, then codes {codes}",
        )

    return suite
