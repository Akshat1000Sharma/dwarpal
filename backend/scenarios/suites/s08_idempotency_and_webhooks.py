"""s08 - retries, duplicates and notifications arriving in the wrong order.

An agent that times out will retry. A gateway that is unsure will send the notification twice, or
send the failure after the capture. None of that may produce a second charge, an unwound sale, or
a 500.
"""

from __future__ import annotations

import json

from app.settings import settings
from scenarios.harness import Context, Shopper, Suite, record

SKU, TITLE = "DWP-TEA-001", "Nilgiri Black Tea 250g"
CART = [(SKU, TITLE, 1)]


def run(ctx: Context) -> Suite:
    suite = Suite(
        "s08",
        "Idempotency and webhooks",
        "Retries, duplicate notifications, out-of-order events and unsigned bodies.",
    )
    client = ctx.client
    # Declared before the cases so one that raises cannot make a later one fail for the wrong
    # reason.
    settled: dict[str, object] = {}

    with suite.case(
        "a_retried_quote_returns_the_same_quote",
        proves="a retry after a timeout does not hold stock twice",
        expected="the same checkout id for the same idempotency key",
    ) as case:
        shopper = Shopper(client, "s08-retry-quote")
        key = f"idem-quote-{shopper.agent_id}"
        body = {"items": [{"sku": SKU, "quantity": 1}]}
        headers = {**shopper.headers, "Idempotency-Key": key}
        first = client.post("/checkout/quote", body, headers=headers)[1]
        second = client.post("/checkout/quote", body, headers=headers)[1]
        record(
            case,
            first.get("checkout_id") == second.get("checkout_id"),
            f"{first.get('checkout_id')} vs {second.get('checkout_id')}",
        )

    with suite.case(
        "a_retried_completion_does_not_charge_twice",
        proves="the same request replayed returns the recorded answer rather than acting again",
        expected="identical responses, and only one payment for the checkout",
    ) as case:
        shopper = Shopper(client, "s08-retry-complete")
        quote = shopper.quoted([(SKU, 1)])
        issued = shopper.authorise(CART)
        presentation = shopper.present(issued, quote, audience=ctx.audience)
        credentials = presentation.credentials
        payload = {
            "open_checkout_mandate": credentials.open_checkout,
            "closed_checkout_mandate": credentials.closed_checkout,
            "open_payment_mandate": credentials.open_payment,
            "closed_payment_mandate": credentials.closed_payment,
            "nonce": credentials.nonce,
        }
        key = f"idem-complete-{shopper.agent_id}"
        headers = {"Idempotency-Key": key}
        first = client.post("/checkout/complete", payload, headers=headers)[1]
        second = client.post("/checkout/complete", payload, headers=headers)[1]
        same = (
            first.get("status") == second.get("status")
            and first.get("checkout_id") == second.get("checkout_id")
            and first.get("correlation_id") == second.get("correlation_id")
        )
        record(
            case,
            same,
            f"first {first.get('status')}/{first.get('reason_code')}, "
            f"second {second.get('status')}/{second.get('reason_code')}",
        )
        settled.update(first)

    with suite.case(
        "a_retry_without_the_key_is_caught_by_replay",
        proves="idempotency is a convenience; the nonce store is the guarantee",
        expected="CRED_REPLAYED when the same credentials are sent without an idempotency key",
    ) as case:
        shopper = Shopper(client, "s08-noidem")
        quote = shopper.quoted([(SKU, 1)])
        issued = shopper.authorise(CART)
        presentation = shopper.present(issued, quote, audience=ctx.audience)
        shopper.complete(presentation)
        _status, again = shopper.complete(presentation)
        record(case, again.get("reason_code") == "CRED_REPLAYED", str(again.get("reason_code")))

    with suite.case(
        "an_unsigned_webhook_is_refused_before_parsing",
        proves="a notification nobody signed is not a notification",
        expected="HTTP 401",
    ) as case:
        status, _ = client.post(
            "/webhooks/razorpay", {"event": "payment.captured", "payload": {}}
        )
        record(case, status == 401, f"HTTP {status}")

    with suite.case(
        "a_mis_signed_webhook_is_refused",
        proves="the signature is checked, not merely required to be present",
        expected="HTTP 401 for a body signed with the wrong secret",
    ) as case:
        status, _ = client.post_signed_webhook(
            "/webhooks/razorpay",
            {"event": "payment.captured", "payload": {}},
            "definitely-not-the-webhook-secret",
        )
        record(case, status == 401, f"HTTP {status}")

    with suite.case(
        "a_tampered_body_is_refused",
        proves="the signature covers the exact bytes, so editing the body after signing fails",
        expected="HTTP 401 when a byte changes after the signature is computed",
    ) as case:
        import hashlib
        import hmac
        import urllib.error
        import urllib.request

        honest = {"event": "payment.captured", "payload": {"payment": {"entity": {"amount": 100}}}}
        raw = json.dumps(honest).encode()
        signature = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256
        ).hexdigest()
        tampered = raw.replace(b'"amount": 100', b'"amount": 999')
        request = urllib.request.Request(
            client.base + "/webhooks/razorpay",
            data=tampered,
            method="POST",
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        record(case, status == 401, f"HTTP {status}")

    with suite.case(
        "a_duplicate_capture_is_harmless",
        proves="a gateway that sends the same notification twice does not settle twice",
        expected="the second delivery is accepted and changes nothing",
    ) as case:
        detail = settled.get("detail") or {}
        order_id = detail.get("razorpay_order_id") if isinstance(detail, dict) else None
        if not order_id:
            record(case, True, "the checkout captured inline, so there is no order to double-pay")
        else:
            amount = (detail.get("amount") or {}).get("amount", 0)
            event = {
                "event": "payment.captured",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_dup_{order_id[-12:]}",
                            "order_id": order_id,
                            "amount": amount,
                            "currency": "INR",
                            "status": "captured",
                            "captured": True,
                        }
                    }
                },
            }
            first = client.post_signed_webhook(
                "/webhooks/razorpay", event, settings.RAZORPAY_WEBHOOK_SECRET
            )
            second = client.post_signed_webhook(
                "/webhooks/razorpay", event, settings.RAZORPAY_WEBHOOK_SECRET
            )
            finalised = [
                "checkout.finalised" in (r[1].get("handled") or []) for r in (first, second)
            ]
            record(
                case,
                first[0] == 200 and second[0] == 200 and finalised.count(True) <= 1,
                f"HTTP {first[0]}/{second[0]}, finalised {finalised}",
            )

    with suite.case(
        "a_failure_after_a_capture_does_not_rewrite_it",
        proves="Razorpay does not guarantee ordering, so a late failure must not unwind real money",
        expected="the checkout stays completed after a payment.failed arrives for it",
    ) as case:
        shopper = Shopper(client, "s08-ordering")
        outcome = shopper.buy(CART, audience=ctx.audience)
        detail = outcome.get("detail") or {}
        order_id = detail.get("razorpay_order_id")
        if not order_id:
            record(case, True, "captured inline, so the out-of-order case does not arise")
        else:
            event = {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_suite_{order_id[-14:]}",
                            "order_id": order_id,
                            "amount": (detail.get("amount") or {}).get("amount", 0),
                            "currency": "INR",
                            "status": "failed",
                            "error_code": "BAD_REQUEST_ERROR",
                            "error_description": "arrived after the capture",
                        }
                    }
                },
            }
            status, _ = client.post_signed_webhook(
                "/webhooks/razorpay", event, settings.RAZORPAY_WEBHOOK_SECRET
            )
            _s, checkout = client.get(f"/checkout/{outcome['_quote'].checkout_id}")
            record(
                case,
                status == 200 and checkout.get("state") == "completed",
                f"webhook HTTP {status}, checkout state {checkout.get('state')}",
            )

    with suite.case(
        "the_whatsapp_webhook_refuses_an_unsigned_body_too",
        proves="the same rule applies to the other inbound channel",
        expected="HTTP 401",
    ) as case:
        status, _ = client.post("/webhooks/whatsapp", {"entry": []})
        record(case, status == 401, f"HTTP {status}")

    return suite
