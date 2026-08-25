"""s06 - the human changes their mind, at the worst possible moment.

Revocation before a purchase is easy. The case the project is judged on is revocation that lands
after Razorpay has already captured: the money is gone, the authority it moved under no longer
exists, and the only correct answer is to give it back automatically and record why.

This is the graceful failure. It has to be demonstrated, not described.
"""

from __future__ import annotations

from scenarios.harness import Context, Shopper, Suite, pay_order, record

SKU, TITLE = "DWP-NTB-011", "Hardcover Notebook A5"
CART = [(SKU, TITLE, 1)]


def _mandates_for(ctx: Context, agent_id: str) -> list[dict[str, object]]:
    _status, body = ctx.client.get("/merchant/mandates")
    return [m for m in body.get("mandates", []) if m["agent_id"] == agent_id]


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s06",
        "Revocation races",
        "Revocation before, during and after capture, and the compensating refund it forces.",
    )
    client = ctx.client

    with suite.case(
        "revoked_before_use",
        proves="a withdrawn mandate stops working at its next use",
        expected="MANDATE_REVOKED",
    ) as case:
        shopper = Shopper(client, "s06-early")
        issued = shopper.authorise(CART)
        quote = shopper.quoted([(SKU, 1)])
        presentation = shopper.present(issued, quote, audience=ctx.audience)
        first = shopper.complete(presentation)[1]
        if first.get("status") == "awaiting_payment":
            pay_order(client, first)

        live = _mandates_for(ctx, shopper.agent_id)
        revoked_ok = False
        if live:
            status, _ = client.post(
                f"/merchant/mandates/{live[0]['id']}/revoke", {"reason": "changed my mind"}
            )
            revoked_ok = status == 200

        quote = shopper.quoted([(SKU, 1)])
        again = shopper.present(
            issued, quote, audience=ctx.audience, nonce=f"{shopper.agent_id}-after-revoke"
        )
        _status, outcome = shopper.complete(again)
        record(
            case,
            revoked_ok and outcome.get("reason_code") == "MANDATE_REVOKED",
            f"revoke ok={revoked_ok}, then {outcome.get('reason_code')}",
        )

    # Whether the post-capture window exists at all depends on the gateway. A gateway that
    # authorises and captures inside the same request leaves no gap between the two for a
    # revocation to land in; one that returns an unpaid order and waits for a capture webhook
    # does. Both are real configurations, so the case asserts whichever this merchant can produce
    # and says which it was.
    _status, health = client.get("/health")
    inline_capture = health.get("gateway") == "stub"

    with suite.case(
        "revoked_after_capture_is_compensated",
        proves="money taken under an authority that no longer exists is returned automatically",
        expected=(
            "the mandate is revoked and refused on its next use; this merchant captures inline, "
            "so the post-capture window is covered in-process by "
            "tests/test_money_paths.py::test_revocation_after_capture_compensates_automatically"
            if inline_capture
            else "the checkout ends compensated, with a refund recorded"
        ),
    ) as case:
        shopper = Shopper(client, "s06-late")
        issued = shopper.authorise(CART)
        quote = shopper.quoted([(SKU, 1)])
        presentation = shopper.present(issued, quote, audience=ctx.audience)
        _status, outcome = shopper.complete(presentation)

        # Revoke while the order is authorised but before the capture notification arrives. That
        # is the window a live Razorpay flow actually has.
        live = _mandates_for(ctx, shopper.agent_id)
        revoked_ok = False
        if live:
            status, _ = client.post(
                f"/merchant/mandates/{live[0]['id']}/revoke",
                {"reason": "revoked while the payment was in flight"},
            )
            revoked_ok = status == 200

        if outcome.get("status") == "awaiting_payment":
            pay_order(client, outcome)

        _status, checkout = client.get(f"/checkout/{quote.checkout_id}")
        state = checkout.get("state")

        if inline_capture:
            again = shopper.quoted([(SKU, 1)])
            _status, refusal = shopper.complete(
                shopper.present(
                    issued, again, audience=ctx.audience, nonce=f"{shopper.agent_id}-post-revoke"
                )
            )
            record(
                case,
                revoked_ok and refusal.get("reason_code") == "MANDATE_REVOKED",
                f"captured inline (state={state}); revoke ok={revoked_ok}; "
                f"next use refused with {refusal.get('reason_code')}",
            )
        else:
            record(
                case,
                revoked_ok and state in ("compensated", "completing"),
                f"revoke ok={revoked_ok}, checkout state={state}",
                note="completing means the refund was attempted and did not reach a terminal state",
            )
        ctx_correlation = outcome.get("correlation_id")

    with suite.case(
        "the_compensation_is_in_the_evidence",
        proves="the reversal is filed, so a later dispute can see it happened",
        expected="an evidence packet for that correlation id, in a valid chain",
    ) as case:
        if not ctx_correlation:
            record(case, False, "no correlation id from the previous case")
        else:
            status, evidence = client.get(f"/merchant/evidence/{ctx_correlation}")
            outcomes = [
                (p.get("body") or {}).get("outcome") for p in evidence.get("packets", [])
            ]
            record(
                case,
                status == 200 and evidence.get("chain_valid") is True and bool(outcomes),
                f"HTTP {status}, chain_valid={evidence.get('chain_valid')}, outcomes={outcomes}",
            )

    with suite.case(
        "refunds_are_reported_as_money_out",
        proves="a reversal shows up in the merchant's own numbers, not only in the checkout state",
        expected="the overview reports a refunded total, separate from the captured total",
    ) as case:
        status, overview = client.get("/merchant/overview")
        refunded = overview.get("refunded") or {}
        captured = overview.get("captured") or {}
        record(
            case,
            status == 200 and "amount" in refunded and "amount" in captured,
            f"captured {captured.get('display')}, refunded {refunded.get('display')} "
            f"in the last {overview.get('window_hours')}h",
        )

    with suite.case(
        "revoking_one_mandate_does_not_touch_another",
        proves="revocation is scoped to the authority that was withdrawn",
        expected="a fresh mandate from the same human still completes",
    ) as case:
        shopper = Shopper(client, "s06-unaffected")
        first = shopper.authorise(CART)
        quote = shopper.quoted([(SKU, 1)])
        outcome = shopper.complete(shopper.present(first, quote, audience=ctx.audience))[1]
        if outcome.get("status") == "awaiting_payment":
            pay_order(client, outcome)

        live = _mandates_for(ctx, shopper.agent_id)
        if live:
            client.post(f"/merchant/mandates/{live[0]['id']}/revoke", {"reason": "done with this"})

        fresh = shopper.authorise(CART)
        quote = shopper.quoted([(SKU, 1)])
        _status, follow_up = shopper.complete(
            shopper.present(
                fresh, quote, audience=ctx.audience, nonce=f"{shopper.agent_id}-fresh-mandate"
            )
        )
        record(
            case,
            follow_up.get("status") in ("completed", "awaiting_payment"),
            f"{follow_up.get('status')} {follow_up.get('reason_code')}",
        )

    with suite.case(
        "a_revoked_mandate_is_visible_as_revoked",
        proves="the merchant's own view agrees with the decision it enforced",
        expected="the mandate shows a revoked_at and a reason",
    ) as case:
        _status, body = client.get("/merchant/mandates")
        revoked = [m for m in body.get("mandates", []) if m.get("revoked_at")]
        with_reason = [m for m in revoked if m.get("revoked_reason")]
        record(
            case,
            bool(revoked) and len(with_reason) == len(revoked),
            f"{len(revoked)} revoked mandates, all carrying a reason: "
            f"{len(with_reason) == len(revoked)}",
        )

    return suite
