"""Send one message through each approved template, to prove the template path itself delivers.

`check-channels` proves a template exists and is APPROVED. It cannot prove a send using that
template is accepted, because Meta reports that per message and only at send time: a template can
be approved and still be refused for a parameter count that does not match, a locale that does not
match, or a button index the template does not have. This sends one of each and returns the
message ids, so the claim rests on a real send rather than on the template's status.

It sends to a real phone, so nothing here runs without an explicit opt-in from the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.escalation.whatsapp import build_approval_template_message
from app.notify.messages import build_receipt_template_message
from app.settings import settings

# The probe is not a purchase and must never read as one, so the parameters that carry meaning say
# what this is. The amount and cart are placeholders the template requires, not a real order.
PROBE_AMOUNT_MINOR = 100
PROBE_CURRENCY = "INR"
PROBE_CART = "1 x delivery test, no order was placed"
PROBE_CONSTRAINT = "a delivery test, nothing needs approving"
PROBE_OUTCOME = "A delivery test. No purchase was made and nothing was charged."


class Transport(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProbeResult:
    label: str
    template: str
    language: str
    ok: bool
    message_id: str = ""
    error: str = ""


def _message_id(response: dict[str, Any]) -> str:
    messages = response.get("messages") or []
    return str(messages[0].get("id", "")) if messages else ""


def send_template_probe(
    transport: Transport | None = None, *, to_number: str | None = None
) -> list[ProbeResult]:
    """Send one approval template and one receipt template. Never raises for a failed send.

    A failure is a result, not an exception: the point of the probe is to report what Meta said,
    and one template failing while the other succeeds is exactly the case worth seeing.
    """
    if transport is None:
        from app.escalation.whatsapp import CloudApiTransport

        transport = CloudApiTransport()

    recipient = to_number or settings.ESCALATION_HUMAN_WHATSAPP
    if not recipient:
        return [
            ProbeResult("escalation", settings.META_TEMPLATE_NAME, "", False,
                        error="ESCALATION_HUMAN_WHATSAPP is not set"),
            ProbeResult("receipt", settings.META_RECEIPT_TEMPLATE_NAME, "", False,
                        error="ESCALATION_HUMAN_WHATSAPP is not set"),
        ]

    reference = f"probe-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    merchant = settings.MERCHANT_NAME
    currency = PROBE_CURRENCY

    plans: list[tuple[str, str, str, dict[str, Any] | None]] = []

    if settings.META_TEMPLATE_NAME:
        plans.append((
            "escalation",
            settings.META_TEMPLATE_NAME,
            settings.META_TEMPLATE_LANGUAGE,
            build_approval_template_message(
                to_number=recipient,
                escalation_id=reference,
                merchant_name=merchant,
                amount_minor=PROBE_AMOUNT_MINOR,
                currency=currency,
                cart_summary=PROBE_CART,
                constraint_text=PROBE_CONSTRAINT,
                template_name=settings.META_TEMPLATE_NAME,
                language_code=settings.META_TEMPLATE_LANGUAGE,
            ),
        ))
    else:
        plans.append(("escalation", "", "", None))

    if settings.META_RECEIPT_TEMPLATE_NAME:
        plans.append((
            "receipt",
            settings.META_RECEIPT_TEMPLATE_NAME,
            settings.META_RECEIPT_TEMPLATE_LANGUAGE,
            build_receipt_template_message(
                to_number=recipient,
                template_name=settings.META_RECEIPT_TEMPLATE_NAME,
                language_code=settings.META_RECEIPT_TEMPLATE_LANGUAGE,
                merchant_name=merchant,
                amount_minor=PROBE_AMOUNT_MINOR,
                currency=currency,
                cart_summary=PROBE_CART,
                outcome_text=PROBE_OUTCOME,
                reference=reference,
            ),
        ))
    else:
        plans.append(("receipt", "", "", None))

    results: list[ProbeResult] = []
    for label, name, language, payload in plans:
        if payload is None:
            results.append(
                ProbeResult(label, name, language, False, error="no template name configured")
            )
            continue
        try:
            response = transport.send(payload)
        # A failed send is a result here, not an exception.
        except Exception as exc:
            results.append(ProbeResult(label, name, language, False, error=_describe(exc)))
            continue
        message_id = _message_id(response)
        results.append(
            ProbeResult(
                label,
                name,
                language,
                bool(message_id),
                message_id=message_id,
                error="" if message_id else "no message id in the response",
            )
        )
    return results


def _describe(exc: Exception) -> str:
    """Meta puts the useful part in the response body, which the exception string omits."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            error = response.json().get("error", {})
        # A non-JSON body is still worth reporting, as text.
        except Exception:
            return f"{type(exc).__name__}: {str(exc)[:200]}"
        parts = [str(error.get("message", ""))]
        if error.get("error_data", {}).get("details"):
            parts.append(str(error["error_data"]["details"]))
        if error.get("code"):
            parts.append(f"code {error['code']}")
        return " | ".join(p for p in parts if p)[:300]
    return f"{type(exc).__name__}: {str(exc)[:200]}"
