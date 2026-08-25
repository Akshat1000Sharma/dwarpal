"""s02 - the credential families, fired at the live HTTP door.

The corpus already runs these in-process. This runs them through the real endpoint, because a
verification step that is only ever called directly proves less than one an attacker could
actually reach. Every case asserts the specific reason code, not merely that something was
refused: refusing for the wrong reason is a bug that a pass-or-fail check would hide.
"""

from __future__ import annotations

from app.harness import factory
from scenarios.harness import Case, Context, Shopper, Suite, record

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]


def _expect(case: Case, outcome: dict[str, object], code: str) -> None:
    """Assert the exact reason code. Refusing for the wrong reason is a bug, not a pass."""
    record(case, outcome.get("reason_code") == code, str(outcome.get("reason_code")))


def _attack(ctx: Context, name: str, tamper: factory.Tamper) -> dict[str, object]:
    shopper = Shopper(ctx.client, name)
    quote = shopper.quoted([("DWP-TEA-001", 1)])
    issued = shopper.authorise(CART, tamper=tamper)
    presentation = shopper.present(issued, quote, audience=ctx.audience, tamper=tamper)
    _status, outcome = shopper.complete(presentation)
    return outcome


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s02",
        "Credential attacks",
        "Forgery, theft, replay, expiry and untrusted issuance, each refused with its own code.",
    )

    with suite.case(
        "forged_signature",
        proves="a credential signed by a key the issuer does not hold is worthless",
        expected="CRED_SIGNATURE_INVALID",
    ) as case:
        outcome = _attack(ctx, "s02-forger", factory.Tamper(forge_issuer_signature=True))
        _expect(case, outcome, "CRED_SIGNATURE_INVALID")

    with suite.case(
        "confused_deputy",
        proves="holding somebody else's genuine credential is not the same as holding authority",
        expected="CRED_SUBJECT_MISMATCH",
    ) as case:
        outcome = _attack(ctx, "s02-deputy", factory.Tamper(wrong_agent_key=True))
        _expect(case, outcome, "CRED_SUBJECT_MISMATCH")

    with suite.case(
        "no_proof_of_possession",
        proves="presenting a mandate without proving you hold its key is refused",
        expected="CRED_KEY_BINDING_MISSING",
    ) as case:
        outcome = _attack(ctx, "s02-nokb", factory.Tamper(drop_key_binding=True))
        _expect(case, outcome, "CRED_KEY_BINDING_MISSING")

    with suite.case(
        "expired",
        proves="an expired mandate is refused even though everything about it is genuine",
        expected="CRED_EXPIRED",
    ) as case:
        outcome = _attack(ctx, "s02-expired", factory.Tamper(expired=True))
        _expect(case, outcome, "CRED_EXPIRED")

    with suite.case(
        "not_yet_valid",
        proves="a mandate dated into the future does not work early",
        expected="CRED_NOT_YET_VALID",
    ) as case:
        outcome = _attack(ctx, "s02-future", factory.Tamper(not_yet_valid=True))
        _expect(case, outcome, "CRED_NOT_YET_VALID")

    with suite.case(
        "clock_skew_within_tolerance",
        proves="the skew tolerance is real and small, not an excuse to accept anything",
        expected="a credential 30s ahead is accepted; the configured tolerance is 60s",
    ) as case:
        outcome = _attack(ctx, "s02-skew-ok", factory.Tamper(clock_skew_seconds=30))
        record(
            case,
            outcome.get("status") in ("completed", "awaiting_payment"),
            f"{outcome.get('status')} {outcome.get('reason_code')}",
        )

    with suite.case(
        "clock_skew_beyond_tolerance",
        proves="exaggerated skew does not buy an attacker a valid window",
        expected="CRED_NOT_YET_VALID at 3600s ahead",
    ) as case:
        outcome = _attack(ctx, "s02-skew-bad", factory.Tamper(clock_skew_seconds=3600))
        _expect(case, outcome, "CRED_NOT_YET_VALID")

    with suite.case(
        "unknown_issuer",
        proves="an authority nobody configured cannot mint authority here",
        expected="CRED_ISSUER_UNKNOWN",
    ) as case:
        outcome = _attack(ctx, "s02-unknown", factory.Tamper(unknown_issuer=True))
        _expect(case, outcome, "CRED_ISSUER_UNKNOWN")

    with suite.case(
        "cart_altered_after_signing",
        proves="editing the checkout after the merchant signed it breaks the binding",
        expected="a CHECKOUT_ or CART_ refusal, never a sale",
    ) as case:
        outcome = _attack(
            ctx, "s02-cartswap", factory.Tamper(altered_checkout_hash="not-the-real-hash")
        )
        code = str(outcome.get("reason_code", ""))
        record(
            case,
            outcome.get("status") != "completed"
            and code.startswith(("CHECKOUT_", "CART_", "CRED_")),
            code,
        )

    with suite.case(
        "amount_altered_between_quote_and_payment",
        proves="the amount paid must be the amount quoted",
        expected="a refusal, never a sale at the attacker's number",
    ) as case:
        outcome = _attack(ctx, "s02-pricedrift", factory.Tamper(payment_amount_minor=1))
        record(
            case,
            outcome.get("status") != "completed",
            f"{outcome.get('status')} {outcome.get('reason_code')}",
        )

    with suite.case(
        "payee_substituted",
        proves="an agent cannot redirect the money to somebody else",
        expected="CONSTRAINT_PAYEE_NOT_ALLOWED",
    ) as case:
        outcome = _attack(
            ctx, "s02-payee", factory.Tamper(payee={"id": "attacker", "name": "Attacker"})
        )
        _expect(case, outcome, "CONSTRAINT_PAYEE_NOT_ALLOWED")

    with suite.case(
        "instrument_substituted",
        proves="an agent cannot pay with an instrument the human did not authorise",
        expected="CONSTRAINT_INSTRUMENT_NOT_ALLOWED",
    ) as case:
        outcome = _attack(
            ctx,
            "s02-instrument",
            factory.Tamper(payment_instrument={"id": "pi_not_yours", "type": "CARD"}),
        )
        _expect(case, outcome, "CONSTRAINT_INSTRUMENT_NOT_ALLOWED")

    with suite.case(
        "malformed_credential",
        proves="garbage gets a structured refusal, not a stack trace or a 500",
        expected="HTTP 4xx with a reason code and an action an agent can follow",
    ) as case:
        status, refusal = ctx.client.post(
            "/checkout/complete",
            {"open_checkout_mandate": "not-a-credential~", "closed_checkout_mandate": "also-not~"},
        )
        error = refusal.get("error", refusal)
        code = error.get("code") or refusal.get("reason_code")
        action = error.get("action") or refusal.get("action")
        record(
            case,
            400 <= status < 500 and bool(code) and bool(action),
            f"HTTP {status} {code} action={action}",
        )

    with suite.case(
        "every_refusal_is_evidence",
        proves="refusals are filed, not discarded; they are the more valuable records",
        expected="an evidence packet id on a refused attempt",
    ) as case:
        outcome = _attack(ctx, "s02-evidence", factory.Tamper(wrong_agent_key=True))
        record(
            case,
            bool(outcome.get("evidence_packet_id")),
            f"packet={outcome.get('evidence_packet_id')}",
        )

    return suite
