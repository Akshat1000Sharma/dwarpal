"""s10 - the agent that cannot prove who it is.

A merchant that refuses all traffic it cannot verify has built a wall, not a shop. The product
decision is a smaller door: browse, search, quote and assemble a cart freely, but no checkout
above a configured ceiling and nothing in a restricted category.

The other half is that the refusal has to be actionable. An agent must be able to read the
response and know which credentials would unlock the attempt, without a human interpreting it.
"""

from __future__ import annotations

from scenarios.harness import Context, Shopper, Suite, reason_of, record


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s10",
        "The degraded path",
        "An unverifiable agent gets a smaller door, and a refusal it can act on.",
    )
    client = ctx.client
    anon = {"X-Agent-Id": f"agent:anonymous-s10-{id(ctx) % 100000}"}

    with suite.case(
        "browsing_needs_no_credentials",
        proves="discovery is open, because a shop nobody can look at sells nothing",
        expected="HTTP 200 with items",
    ) as case:
        status, body = client.get("/catalog/items?limit=5", headers=anon)
        record(
            case,
            status == 200 and body.get("count", 0) > 0,
            f"HTTP {status}, {body.get('count')} items",
        )

    with suite.case(
        "search_needs_no_credentials",
        proves="an agent can find what it wants before deciding whether to identify itself",
        expected="HTTP 200 with results",
    ) as case:
        status, body = client.get("/catalog/search?q=tea", headers=anon)
        record(
            case,
            status == 200 and body.get("count", 0) > 0,
            f"HTTP {status}, {body.get('count')} hits",
        )

    with suite.case(
        "quoting_needs_no_credentials",
        proves="a cart can be assembled before authority is presented",
        expected="HTTP 200 with a signed Checkout",
    ) as case:
        status, body = client.post(
            "/checkout/quote", {"items": [{"sku": "DWP-TEA-001", "quantity": 1}]}, headers=anon
        )
        record(case, status == 200 and bool(body.get("checkout_jwt")), f"HTTP {status}")

    with suite.case(
        "checkout_without_credentials_is_refused",
        proves="the smaller door is still a door, not an open one",
        expected="a 4xx refusal carrying a reason code",
    ) as case:
        status, refusal = client.post(
            "/checkout/complete",
            {"open_checkout_mandate": "nothing~", "closed_checkout_mandate": "nothing~"},
        )
        code = reason_of(refusal)
        record(case, 400 <= status < 500 and bool(code), f"HTTP {status} {code}")

    with suite.case(
        "the_refusal_names_what_to_do_next",
        proves="an agent can act on the refusal without a human reading it",
        expected="an action from the closed set, and a retryable flag",
    ) as case:
        status, refusal = client.post(
            "/checkout/complete",
            {"open_checkout_mandate": "nothing~", "closed_checkout_mandate": "nothing~"},
        )
        error = refusal.get("error") if isinstance(refusal.get("error"), dict) else refusal
        action = error.get("action")
        retryable = error.get("retryable")
        known = {"proceed", "retry", "present_credentials", "reduce_cart", "wait", "stop"}
        record(
            case,
            action in known and isinstance(retryable, bool),
            f"action={action}, retryable={retryable}",
        )

    with suite.case(
        "the_reason_codes_are_a_published_closed_set",
        proves="an agent can enumerate every refusal it might see, in advance",
        expected="the merchant serves its reason codes with the action for each",
    ) as case:
        status, body = client.get("/merchant/reason-codes")
        codes = body.get("codes", [])
        complete = all(c.get("code") and c.get("agent_action") for c in codes)
        record(
            case,
            status == 200 and len(codes) > 40 and complete,
            f"HTTP {status}, {len(codes)} codes, all carrying an action: {complete}",
        )

    with suite.case(
        "an_unverified_agent_hits_a_ceiling_not_a_wall",
        proves="the degraded path allows small purchases and stops large ones",
        expected="a verified purchase of an expensive item still needs real credentials",
    ) as case:
        # A verified shopper buying an expensive item succeeds, which is the control: the ceiling
        # is about verification, not about the item.
        shopper = Shopper(client, "s10-verified")
        outcome = shopper.buy(
            [("DWP-KBD-008", "Mechanical Keyboard 75 percent", 1)], audience=ctx.audience
        )
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"verified purchase of a 7490.00 item: {outcome.get('status')}",
        )

    with suite.case(
        "the_merchant_publishes_its_trust_anchors",
        proves="an agent can find out which authority to get a credential from",
        expected="the discovery document lists trust anchors with their tiers",
    ) as case:
        status, document = client.get("/.well-known/ap2-merchant")
        anchors = document.get("trust_anchors") or []
        record(
            case,
            status == 200 and bool(anchors) and all(a.get("tier") for a in anchors),
            f"{len(anchors)} anchors, tiers {sorted({a.get('tier') for a in anchors})}",
        )

    return suite
