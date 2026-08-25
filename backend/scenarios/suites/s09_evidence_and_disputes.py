"""s09 - what the evidence is actually worth.

An append-only log is only useful if somebody can check it without trusting the thing that wrote
it, and if it changes the outcome of an argument. This suite checks the chain grows and verifies,
and then asks the dispute responder to defend real transactions and to admit when it cannot.

A responder that recommends contesting everything is worthless, so the case that matters most here
is the one where the merchant holds evidence and still says refund.
"""

from __future__ import annotations

from scenarios.harness import Context, Shopper, Suite, record

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s09",
        "Evidence and disputes",
        "Chain growth and verification, then whether the evidence wins or honestly loses.",
    )
    client = ctx.client
    # Declared before the cases so one that raises cannot make a later one fail for the wrong
    # reason.
    defensible_correlation = ""

    with suite.case(
        "the_chain_grows_and_verifies",
        proves="packets are hash chained, so a retroactive edit is detectable",
        expected="the merchant reports a valid chain over the packets written so far",
    ) as case:
        status, index = client.get("/merchant/evidence?limit=50")
        chain = index.get("chain") or {}
        record(
            case,
            status == 200 and chain.get("valid") is True and len(index.get("packets", [])) > 0,
            f"{len(index.get('packets', []))} packets, valid={chain.get('valid')}, "
            f"problems={chain.get('problems')}",
        )

    with suite.case(
        "a_packet_reconstructs_what_the_buyer_was_shown",
        proves="the packet is a snapshot, not a pointer at a record that can change underneath it",
        expected="the packet carries a catalog snapshot and the acknowledged policy hash",
    ) as case:
        shopper = Shopper(client, "s09-snapshot")
        outcome = shopper.buy(CART, audience=ctx.audience)
        correlation = outcome.get("correlation_id")
        status, evidence = client.get(f"/merchant/evidence/{correlation}")
        bodies = [p.get("body") or {} for p in evidence.get("packets", [])]
        has_snapshot = any(
            (b.get("checkout") or {}).get("catalog_snapshot") is not None for b in bodies
        )
        has_policy = any((b.get("checkout") or {}).get("policy_hash") for b in bodies)
        record(
            case,
            status == 200 and has_snapshot and has_policy,
            f"HTTP {status}, snapshot={has_snapshot}, policy_hash={has_policy}",
        )
        defensible_correlation = str(correlation or "")

    with suite.case(
        "a_refusal_is_filed_as_well_as_a_sale",
        proves="the refusals are the more valuable evidence, so they are never dropped",
        expected="refused outcomes appear in the evidence index",
    ) as case:
        _status, index = client.get("/merchant/evidence?limit=200")
        outcomes = {p.get("outcome") for p in index.get("packets", [])}
        refusals = {o for o in outcomes if o and o.startswith("refused")}
        record(case, bool(refusals), f"outcomes present: {sorted(o for o in outcomes if o)}")

    with suite.case(
        "a_well_evidenced_transaction_is_defensible",
        proves="the evidence packet changes the answer to a dispute",
        expected="a contest recommendation with a strong score",
    ) as case:
        if not defensible_correlation:
            record(case, False, "the previous case did not produce a transaction to dispute")
            raise RuntimeError("no correlation id")
        status, created = client.post(
            "/merchant/disputes",
            {
                "correlation_id": defensible_correlation,
                "claim": "the cardholder states this purchase was never authorised",
            },
        )
        dispute_id = created.get("id")
        _s, detail = client.get(f"/merchant/disputes/{dispute_id}")
        record(
            case,
            status == 200
            and detail.get("recommendation") == "contest"
            and (detail.get("strength_score") or 0) >= 50,
            f"recommendation={detail.get('recommendation')}, "
            f"score={detail.get('strength_score')}",
        )

    with suite.case(
        "a_transaction_with_nothing_behind_it_is_not_defensible",
        proves="the responder knows which fights not to pick",
        expected="a refund recommendation for a correlation id with no evidence",
    ) as case:
        status, created = client.post(
            "/merchant/disputes",
            {
                "correlation_id": "dwc_this_transaction_never_happened",
                "claim": "the cardholder states this purchase was never authorised",
            },
        )
        _s, detail = client.get(f"/merchant/disputes/{created.get('id')}")
        record(
            case,
            status == 200 and detail.get("recommendation") == "refund",
            f"recommendation={detail.get('recommendation')}, "
            f"score={detail.get('strength_score')}",
        )

    with suite.case(
        "the_representment_says_what_it_could_not_show",
        proves="the weaknesses are stated, not omitted; a one-sided representment is a lie",
        expected="the weak case carries a non-empty weaknesses list",
    ) as case:
        _status, disputes = client.get("/merchant/disputes")
        weak = [d for d in disputes.get("disputes", []) if d.get("recommendation") == "refund"]
        detail = {}
        if weak:
            _s, detail = client.get(f"/merchant/disputes/{weak[0]['id']}")
        weaknesses = (detail.get("representment") or {}).get("weaknesses") or []
        record(
            case,
            bool(weak) and bool(weaknesses),
            f"{len(weak)} refund recommendations, first lists {len(weaknesses)} weaknesses",
        )

    with suite.case(
        "a_dispute_decision_is_recorded",
        proves="the merchant's own choice is part of the record, not just the recommendation",
        expected="deciding a dispute sets its outcome and a timestamp",
    ) as case:
        _status, disputes = client.get("/merchant/disputes")
        rows = disputes.get("disputes", [])
        if not rows:
            record(case, False, "no disputes to decide")
        else:
            status, decided = client.post(
                f"/merchant/disputes/{rows[0]['id']}/decide", {"outcome": "contested"}
            )
            record(
                case,
                status == 200
                and decided.get("outcome") == "contested"
                and bool(decided.get("decided_at")),
                f"HTTP {status}, outcome={decided.get('outcome')}",
            )

    with suite.case(
        "the_chain_still_verifies_after_all_of_that",
        proves="nothing in the dispute path mutates the append-only log",
        expected="the chain is still valid at the end of the suite",
    ) as case:
        status, index = client.get("/merchant/evidence?limit=100")
        chain = index.get("chain") or {}
        record(
            case,
            status == 200 and chain.get("valid") is True,
            f"valid={chain.get('valid')}, problems={chain.get('problems')}",
        )

    return suite
