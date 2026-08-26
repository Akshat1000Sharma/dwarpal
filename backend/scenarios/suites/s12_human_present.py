"""s12 - the human-present flow, over the wire.

AP2 has two flows. Everything else in this suite is the human-not-present one, where the merchant's
verification duty is hardest. This is the other: a person is at a trusted surface, and the surface
says so.

The property being demonstrated is that presence is a credential rather than a courtesy. It is
verified like any other, and it moves nothing: every case here that ends in a refusal would be
refused identically with no person in the room. The one thing it does change is who answers a
question the kernel could not settle, and that answer still has to be signed.
"""

from __future__ import annotations

from app.harness import factory
from app.trust.registry import publish_key
from scenarios.harness import Context, Shopper, Suite, reason_of, record

TEA = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]
HEADPHONES = [("DWP-HDP-007", "Wireless Headphones", 1)]
WINE = [("DWP-WIN-005", "Sula Cabernet Shiraz 750ml", 1)]


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s12",
        "The human-present flow",
        "Presence is verified, bounded and recorded, and it widens nothing.",
    )

    with suite.case(
        "a_present_human_completes_a_purchase",
        proves="the flow works at all, rather than existing only as a way to be refused",
        expected="the purchase settles and the verdict says a human was present",
    ) as case:
        shopper = Shopper(ctx.client, "s12-happy")
        outcome = shopper.buy(TEA, audience=ctx.audience, human_present=True)
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"{outcome.get('status')} {outcome.get('reason_code')}",
        )

    with suite.case(
        "the_absent_flow_is_unchanged",
        proves="adding presence did not alter the flow the rest of this suite tests",
        expected="the same cart with no attestation settles exactly as before",
    ) as case:
        shopper = Shopper(ctx.client, "s12-absent")
        outcome = shopper.buy(TEA, audience=ctx.audience)
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"{outcome.get('status')} {outcome.get('reason_code')}",
        )

    with suite.case(
        "a_forged_attestation_is_refused",
        proves="anyone able to mint one could label any purchase as watched",
        expected="PRESENCE_ATTESTATION_INVALID",
    ) as case:
        shopper = Shopper(ctx.client, "s12-forged")
        outcome = shopper.buy(
            TEA,
            audience=ctx.audience,
            human_present=True,
            tamper=factory.Tamper(forge_presence_signature=True),
            pay=False,
        )
        record(case, reason_of(outcome) == "PRESENCE_ATTESTATION_INVALID", reason_of(outcome))

    with suite.case(
        "an_untrusted_surface_cannot_attest",
        proves="the set of surfaces that may vouch for a person is configuration, not a claim",
        expected="PRESENCE_ISSUER_UNTRUSTED",
    ) as case:
        shopper = Shopper(ctx.client, "s12-untrusted")
        outcome = shopper.buy(
            TEA,
            audience=ctx.audience,
            human_present=True,
            tamper=factory.Tamper(presence_issuer_id=factory.UNKNOWN_ISSUER),
            pay=False,
        )
        record(case, reason_of(outcome) == "PRESENCE_ISSUER_UNTRUSTED", reason_of(outcome))

    with suite.case(
        "a_stale_attestation_is_refused",
        proves="presence is a claim about this moment, so an old one is about a different one",
        expected="PRESENCE_ATTESTATION_STALE",
    ) as case:
        shopper = Shopper(ctx.client, "s12-stale")
        outcome = shopper.buy(
            TEA,
            audience=ctx.audience,
            human_present=True,
            tamper=factory.Tamper(presence_age_seconds=3600),
            pay=False,
        )
        record(case, reason_of(outcome) == "PRESENCE_ATTESTATION_STALE", reason_of(outcome))

    with suite.case(
        "an_attestation_for_another_cart_is_refused",
        proves="a watched purchase of one thing must not become an unwatched purchase of another",
        expected="PRESENCE_BINDING_MISMATCH",
    ) as case:
        shopper = Shopper(ctx.client, "s12-othercart")
        outcome = shopper.buy(
            TEA,
            audience=ctx.audience,
            human_present=True,
            tamper=factory.Tamper(presence_checkout_hash="some-other-checkout"),
            pay=False,
        )
        record(case, reason_of(outcome) == "PRESENCE_BINDING_MISMATCH", reason_of(outcome))

    with suite.case(
        "presence_does_not_lift_the_amount_cap",
        proves="a present person is still bound by what they signed in advance",
        expected="CONSTRAINT_AMOUNT_EXCEEDED, the same refusal an absent buyer gets",
    ) as case:
        shopper = Shopper(ctx.client, "s12-overcap")
        outcome = shopper.buy(
            HEADPHONES,
            audience=ctx.audience,
            amount_cap_minor=100000,
            human_present=True,
            pay=False,
        )
        record(case, reason_of(outcome) == "CONSTRAINT_AMOUNT_EXCEEDED", reason_of(outcome))

    with suite.case(
        "presence_does_not_permit_a_restricted_item",
        proves="the item policy is the merchant's obligation, not the buyer's preference",
        expected="a refusal on the item, not a sale",
    ) as case:
        shopper = Shopper(ctx.client, "s12-restricted")
        outcome = shopper.buy(WINE, audience=ctx.audience, human_present=True, pay=False)
        code = reason_of(outcome)
        record(
            case,
            outcome.get("status") != "completed"
            and code in ("ITEM_AGE_RESTRICTED", "CATEGORY_FORBIDDEN", "ITEM_REGION_LOCKED"),
            code,
        )

    with suite.case(
        "presence_does_not_settle_a_prose_constraint",
        proves="being at the keyboard is not the same as having been asked",
        expected="the checkout escalates rather than completing",
    ) as case:
        shopper = Shopper(ctx.client, "s12-prose")
        outcome = shopper.buy(
            TEA,
            audience=ctx.audience,
            natural_language=["only things we will use this week"],
            human_present=True,
            pay=False,
        )
        record(
            case,
            outcome.get("status") != "completed",
            f"{outcome.get('status')} {reason_of(outcome)}",
        )

    with suite.case(
        "a_signed_confirmation_settles_the_purchase",
        proves=(
            "the whole route from an unresolved constraint to a completed sale, "
            "answered in band by the person who is already there"
        ),
        expected="APPROVED_AFTER_HUMAN_APPROVAL after the confirmation is accepted",
    ) as case:
        shopper = Shopper(ctx.client, "s12-confirm")
        quote = shopper.quoted([("DWP-TEA-001", 1)])
        issued = shopper.authorise(TEA, natural_language=["only things we will use this week"])
        first = shopper.present(issued, quote, audience=ctx.audience, human_present=True)
        _status, escalated = shopper.complete(first)
        escalation_id = (escalated.get("detail") or {}).get("escalation_id")

        if not escalation_id:
            record(case, False, f"no escalation was raised: {reason_of(escalated)}")
        else:
            confirm_status, confirmed = shopper.confirm(escalation_id, quote, "approve")
            second = shopper.present(issued, quote, audience=ctx.audience, human_present=True)
            _status, settled = shopper.complete(second)
            record(
                case,
                confirm_status == 200
                and confirmed.get("accepted") is True
                and reason_of(settled) == "APPROVED_AFTER_HUMAN_APPROVAL",
                f"confirm={confirmed.get('status')} settle={reason_of(settled)}",
            )

    with suite.case(
        "a_confirmation_the_agent_signed_itself_is_refused",
        proves="an approval has to be something only the human's surface could have produced",
        expected="a refusal naming the untrusted signer, never an accepted answer",
    ) as case:
        shopper = Shopper(ctx.client, "s12-selfsign")
        quote = shopper.quoted([("DWP-TEA-001", 1)])
        issued = shopper.authorise(TEA, natural_language=["only things we will use this week"])
        presentation = shopper.present(issued, quote, audience=ctx.audience, human_present=True)
        _status, escalated = shopper.complete(presentation)
        escalation_id = (escalated.get("detail") or {}).get("escalation_id")

        if not escalation_id:
            record(case, False, f"no escalation was raised: {reason_of(escalated)}")
        else:
            impostor = factory.Principals.create(
                agent_id="agent:s12-impostor", issuer_id=factory.UNKNOWN_ISSUER, register=False
            )
            status, refusal = ctx.client.post(
                "/checkout/confirm",
                {
                    "escalation_id": escalation_id,
                    "confirmation": factory.sign_confirmation(
                        impostor,
                        escalation_id=escalation_id,
                        checkout_hash=quote.checkout_hash,
                        decision="approve",
                    ),
                },
            )
            record(
                case,
                status >= 400 and reason_of(refusal) == "PRESENCE_ISSUER_UNTRUSTED",
                f"HTTP {status} {reason_of(refusal)}",
            )

    with suite.case(
        "a_confirmation_from_the_wrong_registered_surface_is_refused",
        proves=(
            "being in the trust registry is not the same as being the surface this person is at; "
            "one authority cannot answer a question put to another"
        ),
        expected="PRESENCE_ISSUER_UNTRUSTED, from an authority the merchant does trust",
    ) as case:
        shopper = Shopper(ctx.client, "s12-wrongsurface")
        quote = shopper.quoted([("DWP-TEA-001", 1)])
        issued = shopper.authorise(TEA, natural_language=["only things we will use this week"])
        presentation = shopper.present(issued, quote, audience=ctx.audience, human_present=True)
        _status, escalated = shopper.complete(presentation)
        escalation_id = (escalated.get("detail") or {}).get("escalation_id")

        if not escalation_id:
            record(case, False, f"no escalation was raised: {reason_of(escalated)}")
        else:
            # A real, registered authority, publishing a real key. It simply is not the surface
            # that issued the mandate this escalation is about.
            other = factory.Principals.create(
                agent_id="agent:s12-other-surface",
                issuer_id=factory.SANDBOX_ISSUER,
                register=False,
            )
            publish_key(other.issuer_id, other.issuer.public_jwk())
            status, refusal = ctx.client.post(
                "/checkout/confirm",
                {
                    "escalation_id": escalation_id,
                    "confirmation": factory.sign_confirmation(
                        other,
                        escalation_id=escalation_id,
                        checkout_hash=quote.checkout_hash,
                        decision="approve",
                    ),
                },
            )
            record(
                case,
                status >= 400 and reason_of(refusal) == "PRESENCE_ISSUER_UNTRUSTED",
                f"HTTP {status} {reason_of(refusal)}",
            )

    with suite.case(
        "an_unanswered_question_still_denies",
        proves="silence fails closed whether or not somebody is sitting there",
        expected="an escalation with a deadline, and no approval without an answer",
    ) as case:
        _status, body = ctx.client.get("/merchant/escalations")
        rows = body.get("escalations", [])
        approved_without_answer = [
            e
            for e in rows
            if e.get("status") == "approved"
            and not any(r.get("accepted") for r in e.get("responses", []))
        ]
        record(
            case,
            not approved_without_answer,
            f"{len(approved_without_answer)} escalations approved with no accepted answer",
        )

    return suite
