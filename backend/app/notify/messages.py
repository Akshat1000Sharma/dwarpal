"""Message bodies for purchase receipts.

Two shapes for each receipt, matching ``app.escalation.whatsapp``: an approved template, which is
the only thing Meta delivers to a quiet inbox, and a free-form text message, which only delivers
inside the 24 hour customer service window. Neither carries a button: a receipt reports something
that has already happened, so there is nothing for the human to answer.
"""

from __future__ import annotations

from typing import Any

from app.escalation.whatsapp import _one_line

# Meta caps a text body at 4096 characters. Staying well inside it keeps the receipt readable on a
# phone rather than merely accepted by the API.
_BODY_LIMIT = 1024


def _money(amount_minor: int, currency: str) -> str:
    return f"{currency} {amount_minor / 100:,.2f}"


def build_purchase_receipt_message(
    *,
    to_number: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    agent_id: str,
    reference: str,
) -> dict[str, Any]:
    body = (
        f"An agent completed a purchase on your behalf at {merchant_name}.\n\n"
        f"Amount: {_money(amount_minor, currency)}\n"
        f"Items: {cart_summary}\n"
        f"Agent: {agent_id}\n\n"
        f"Reference: {reference}\n"
        "Every step of this purchase is recorded and can be replayed."
    )
    return _text(to_number, body)


def build_purchase_refused_message(
    *,
    to_number: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    agent_id: str,
    reason_code: str,
    reference: str,
) -> dict[str, Any]:
    body = (
        f"An agent tried to buy something on your behalf at {merchant_name} and was refused.\n\n"
        f"Amount: {_money(amount_minor, currency)}\n"
        f"Items: {cart_summary}\n"
        f"Agent: {agent_id}\n"
        f"Reason: {reason_code}\n\n"
        f"Reference: {reference}\n"
        "No money moved. The refusal is recorded with the evidence behind it."
    )
    return _text(to_number, body)


def build_purchase_compensated_message(
    *,
    to_number: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    agent_id: str,
    reference: str,
) -> dict[str, Any]:
    body = (
        f"A purchase an agent made on your behalf at {merchant_name} has been reversed.\n\n"
        f"Amount refunded: {_money(amount_minor, currency)}\n"
        f"Items: {cart_summary}\n"
        f"Agent: {agent_id}\n\n"
        "Your authority was withdrawn after the payment had already been taken, so the money was "
        f"returned automatically.\nReference: {reference}"
    )
    return _text(to_number, body)


def build_receipt_template_message(
    *,
    to_number: str,
    template_name: str,
    language_code: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    outcome_text: str,
    reference: str,
) -> dict[str, Any]:
    """The same receipt as an approved template, for delivery outside the 24 hour window.

    Five body parameters in a fixed order: merchant, amount, items, what happened, reference.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": _one_line(merchant_name, 60)},
                        {"type": "text", "text": _money(amount_minor, currency)},
                        {"type": "text", "text": _one_line(cart_summary, 300)},
                        {"type": "text", "text": _one_line(outcome_text, 300)},
                        {"type": "text", "text": _one_line(reference, 64)},
                    ],
                }
            ],
        },
    }


def _text(to_number: str, body: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": body[:_BODY_LIMIT]},
    }
