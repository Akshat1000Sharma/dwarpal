"""s04 - two agents want the last one.

Overselling is the merchant's own loss, so stock is held under a row lock at quote time rather
than checked at capture time. The properties that matter:

    one winner, and the loser gets a structured answer with a substitute where one exists
    a single agent cannot exhaust the shelf by holding carts it never converts
    a loser never sees a 500. An agent that cannot tell "sold out" from "the merchant is broken"
    will retry the wrong thing
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from scenarios.harness import Context, Shopper, Suite, record

# Seeded with a single unit, which is what makes the race real.
SCARCE_SKU, SCARCE_TITLE = "DWP-PEN-012", "Fountain Pen Medium Nib"
# Seeded with three units, and it has a sibling in the same category that the substitute
# finder can offer when it runs out.
LOW_SKU, LOW_TITLE, LOW_STOCK = "DWP-LMP-009", "Desk Lamp with Dimmer", 3


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s04",
        "Inventory contention",
        "Last-unit races, hold quotas, and what a loser is told.",
    )
    client = ctx.client
    # Declared before the first case so a case that raises cannot make the next one fail for the
    # wrong reason. A cascade of unbound-variable errors hides whatever actually went wrong.
    race_results: list[tuple[int, str]] = []

    with suite.case(
        "one_winner_for_the_last_unit",
        proves="the merchant cannot sell stock it does not have",
        expected=(
            f"at most 1 of {ctx.scale.concurrency} concurrent quotes "
            "for a 1-unit SKU succeeds"
        ),
    ) as case:
        def grab(index: int) -> tuple[int, str]:
            shopper = Shopper(client, f"s04-racer-{index}")
            status, body = shopper.quote([(SCARCE_SKU, 1)])
            error = body.get("error", body)
            return status, str(error.get("code", "") if isinstance(error, dict) else "")

        with ThreadPoolExecutor(max_workers=ctx.scale.concurrency) as pool:
            results = list(pool.map(grab, range(ctx.scale.concurrency)))

        winners = [r for r in results if r[0] == 200]
        record(
            case,
            len(winners) <= 1,
            f"{len(winners)} of {len(results)} quotes succeeded; "
            f"losers saw {sorted({c for s, c in results if s != 200})}",
        )
        race_results.extend(results)

    with suite.case(
        "a_loser_never_sees_a_server_error",
        proves="losing a race is a business outcome, not a fault",
        expected="every losing response is 4xx with a reason code",
    ) as case:
        bad = [(s, c) for s, c in race_results if s >= 500 or (s != 200 and not c)]
        record(
            case,
            bool(race_results) and not bad,
            f"{len(bad)} losers answered badly out of {len(race_results)}",
        )

    with suite.case(
        "the_loser_is_offered_a_substitute",
        proves="the merchant answers a sold-out with something it can still sell",
        expected="INVENTORY_UNAVAILABLE carrying a substitute item",
    ) as case:
        # The dimmer lamp is seeded with three units and has a sibling in the same category, so
        # holding all three and asking for a fourth is the case where a substitute exists. Each
        # unit is held by a different agent, because the hold quota is per agent.
        codes: list[str] = []
        detail: dict[str, object] = {}
        for index in range(LOW_STOCK + 1):
            holder = Shopper(client, f"s04-lamp-{index}")
            _status, body = holder.quote([(LOW_SKU, 1)])
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            codes.append(str(error.get("code", "ok")))
            if error.get("code") == "INVENTORY_UNAVAILABLE":
                detail = error.get("detail") or {}
                break
        substitute = (detail.get("substitute") or {}).get("sku") if detail else None
        record(
            case,
            "INVENTORY_UNAVAILABLE" in codes and substitute is not None,
            f"codes {codes}, substitute offered: {substitute}",
        )

    with suite.case(
        "one_agent_cannot_exhaust_the_shelf_with_holds",
        proves="denial of inventory by an agent that never converts is capped per agent",
        expected="HOLD_QUOTA_EXCEEDED once the per-agent quota is reached",
    ) as case:
        hog = Shopper(client, "s04-hog")
        codes: list[str] = []
        for _ in range(8):
            _status, body = hog.quote([("DWP-TEA-001", 1)])
            error = body.get("error", body)
            codes.append(str(error.get("code", "ok")) if isinstance(error, dict) else "ok")
        record(
            case,
            "HOLD_QUOTA_EXCEEDED" in codes,
            f"after {len(codes)} carts the agent saw {sorted(set(codes))}",
        )

    with suite.case(
        "another_agents_holds_are_unaffected",
        proves="the quota is per agent, so one greedy caller does not close the shop",
        expected="a different agent still quotes successfully",
    ) as case:
        polite = Shopper(client, "s04-polite")
        status, _body = polite.quote([("DWP-TEA-001", 1)])
        record(case, status == 200, f"HTTP {status}")

    with suite.case(
        "quantity_outside_the_items_own_range_is_refused",
        proves="the per-item min and max in the catalog are enforced, not decorative",
        expected="QUANTITY_OUT_OF_RANGE above the item's max_order_quantity",
    ) as case:
        shopper = Shopper(client, "s04-toomany")
        _status, body = shopper.quote([("DWP-TEA-001", 99)])
        error = body.get("error", body)
        code = error.get("code") if isinstance(error, dict) else None
        record(case, code == "QUANTITY_OUT_OF_RANGE", str(code))

    return suite
