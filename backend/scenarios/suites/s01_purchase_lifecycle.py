"""s01 - the ordinary purchase, all the way through.

If this suite fails nothing else matters, so it runs first. It walks the human-not-present flow a
step at a time and checks the artifact each step is supposed to produce: a discovery document an
agent can act on, items carrying machine-readable purchase constraints, signed policy terms, a
merchant-signed Checkout, four conformant credentials, a verdict, a capture, a receipt, and an
evidence packet in a valid chain.
"""

from __future__ import annotations

from app.ap2.schema_validation import assert_conforms
from scenarios.harness import Context, Shopper, Suite, pay_order, record

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 2)]


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s01",
        "Purchase lifecycle",
        "One human-not-present purchase, checked at every artifact it is supposed to produce.",
    )
    client = ctx.client

    with suite.case(
        "discovery",
        proves="an arriving agent can learn what this merchant speaks without reading docs",
        expected="a discovery document naming the human-not-present flow and all four vct values",
    ) as case:
        status, document = client.get("/.well-known/ap2-merchant")
        flows = ((document.get("protocols") or {}).get("ap2") or {}).get("flows") or []
        accepted = {c["vct"] for c in document.get("accepted_credentials", [])}
        record(
            case,
            status == 200
            and "human-not-present" in flows
            and accepted
            == {
                "mandate.checkout.open.1",
                "mandate.checkout.1",
                "mandate.payment.open.1",
                "mandate.payment.1",
            },
            f"HTTP {status}, flows {flows}, {len(accepted)} credential types",
        )

    with suite.case(
        "mocked_roles_declared",
        proves="the merchant says which AP2 roles it does not really implement",
        expected="credential_provider listed among the mocked roles",
    ) as case:
        _status, document = client.get("/.well-known/ap2-merchant")
        mocked = document.get("roles_mocked") or []
        record(case, "credential_provider" in mocked, f"roles_mocked={mocked}")

    with suite.case(
        "catalog_constraints",
        proves="catalog items carry purchase constraints a machine can gate on",
        expected="min/max quantity, returnable, age_restricted and region_locked on every item",
    ) as case:
        status, item = client.get("/catalog/items/DWP-TEA-001")
        required = {"min_order_quantity", "returnable", "age_restricted", "region_locked"}
        present = required <= set(item.get("purchase_constraints", {}))
        record(case, status == 200 and present, f"HTTP {status}, constraints present={present}")

    with suite.case(
        "policy_terms_hashed",
        proves="the terms an agent must acknowledge are signed and content-addressed",
        expected="a content hash and a merchant-signed JWT",
    ) as case:
        status, terms = client.get("/policy/terms")
        record(
            case,
            status == 200 and bool(terms.get("content_hash")) and bool(terms.get("signed_jwt")),
            f"HTTP {status}, hash={str(terms.get('content_hash'))[:16]}",
        )

    shopper = Shopper(client, "s01-buyer")

    with suite.case(
        "quote_signed",
        proves="the merchant commits to a price and holds the stock before being asked to sell",
        expected="a merchant-signed Checkout whose policy hash is the live one",
    ) as case:
        _status, terms = client.get("/policy/terms")
        quote = shopper.quoted([(sku, qty) for sku, _t, qty in CART])
        record(
            case,
            bool(quote.checkout_jwt) and quote.policy_hash == terms["content_hash"],
            f"checkout {quote.checkout_id}, {quote.currency} {quote.amount_minor / 100:.2f}",
        )
        ctx_quote = quote

    with suite.case(
        "credentials_conform",
        proves="every credential put on the wire validates against the published AP2 schemas",
        expected="all four validate before being sent",
    ) as case:
        from app.ap2 import sdjwt

        issued = shopper.authorise(CART)
        presentation = shopper.present(issued, ctx_quote, audience=ctx.audience)
        credentials = presentation.credentials
        issuer_jwk = shopper.principals.issuer.public_jwk()
        agent_jwk = shopper.principals.agent.public_jwk()
        checked = 0
        for name, token, key in (
            ("open_checkout_mandate", credentials.open_checkout, issuer_jwk),
            ("checkout_mandate", credentials.closed_checkout, agent_jwk),
            ("open_payment_mandate", credentials.open_payment, issuer_jwk),
            ("payment_mandate", credentials.closed_payment, agent_jwk),
        ):
            claims = sdjwt.verify(token, key).claims
            payload = {
                k: v
                for k, v in claims.items()
                if k not in ("iss", "sub", "nbf", "dwarpal_constraints")
            }
            assert_conforms(name, payload)
            checked += 1
        record(case, checked == 4, f"{checked}/4 credentials validated against the schemas")
        ctx_presentation = presentation

    with suite.case(
        "authority_accepted",
        proves="a well-formed chain inside its authority is approved and an order created",
        expected="status completed or awaiting_payment, with an evidence packet",
    ) as case:
        status, outcome = shopper.complete(ctx_presentation)
        settled = outcome.get("status")
        record(
            case,
            status in (200, 202) and settled in ("completed", "awaiting_payment"),
            f"HTTP {status} {settled} {outcome.get('reason_code')}",
        )
        ctx_outcome = outcome

    with suite.case(
        "evidence_filed",
        proves="every attempt is recorded, whatever it was",
        expected="an evidence packet id on the response",
    ) as case:
        record(
            case,
            bool(ctx_outcome.get("evidence_packet_id")),
            f"packet {ctx_outcome.get('evidence_packet_id')}",
        )

    with suite.case(
        "capture_settles",
        proves="a signed capture from the gateway finalises the checkout",
        expected="the checkout reaches state completed",
    ) as case:
        if ctx_outcome.get("status") == "awaiting_payment":
            paid = pay_order(client, ctx_outcome)
        else:
            paid = True
        _status, checkout = client.get(f"/checkout/{ctx_quote.checkout_id}")
        record(
            case,
            paid and checkout.get("state") == "completed",
            f"paid={paid}, state={checkout.get('state')}",
        )

    with suite.case(
        "receipt_conforms",
        proves="the checkout receipt the merchant signs is a conformant AP2 receipt",
        expected="it validates against the published checkout_receipt schema",
    ) as case:
        receipt = ctx_outcome.get("receipt")
        if receipt is None:
            record(
                case,
                True,
                "no inline receipt: the order was settled by webhook, which is the documented path",
                note="the inline receipt only exists when capture happens in the same request",
            )
        else:
            assert_conforms("checkout_receipt", receipt)
            record(case, True, "the inline receipt validates against the published schema")

    with suite.case(
        "replay_refused",
        proves="a credential that has already been spent cannot be spent again",
        expected="CRED_REPLAYED",
    ) as case:
        _status, replay = shopper.complete(ctx_presentation)
        record(case, replay.get("reason_code") == "CRED_REPLAYED", str(replay.get("reason_code")))

    with suite.case(
        "repeat_purchases",
        proves="the path is repeatable, not a one-off that leaves the merchant in a bad state",
        expected=f"{ctx.scale.purchases} further purchases all settle",
    ) as case:
        settled = 0
        for index in range(ctx.scale.purchases):
            buyer = Shopper(client, f"s01-repeat-{index}")
            outcome = buyer.buy(CART, audience=ctx.audience)
            if outcome.get("status") in ("completed", "awaiting_payment"):
                settled += 1
        record(case, settled == ctx.scale.purchases, f"{settled}/{ctx.scale.purchases} settled")

    return suite
