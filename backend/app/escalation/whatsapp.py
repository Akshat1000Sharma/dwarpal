"""Meta WhatsApp Cloud API transport and inbound webhook verification.

Inbound payloads are authenticated by HMAC-SHA256 over the raw request body against the app
secret, compared in constant time. Anything unsigned or mis-signed is rejected before the body is
parsed, because parsing and re-serialising would break the hash and because an attacker should not
reach the parser at all.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.logging import get_logger
from app.settings import settings

logger = get_logger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="

APPROVE_ID = "dwarpal_approve"
DENY_ID = "dwarpal_deny"


def verify_signature(
    raw_body: bytes, header_value: str | None, app_secret: str | None = None
) -> bool:
    """Constant-time HMAC check over the exact bytes received."""
    if not header_value or not header_value.startswith(_SIGNATURE_PREFIX):
        return False
    secret = app_secret if app_secret is not None else settings.META_APP_SECRET
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len(_SIGNATURE_PREFIX) :])


def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    """Answer Meta's subscription handshake, or refuse it."""
    if mode == "subscribe" and token and hmac.compare_digest(token, settings.META_VERIFY_TOKEN):
        return challenge
    return None


@dataclass(frozen=True)
class InboundAnswer:
    """A parsed human reply. ``answer`` is approve, deny, or unknown."""

    escalation_id: str | None
    answer: str
    message_id: str | None
    from_number: str | None
    raw_text: str = ""


def numbers_in(payload: dict[str, Any]) -> set[str]:
    """Every phone number id this payload carries events for."""
    found: set[str] = set()
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            number = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if number:
                found.add(number)
    return found


def parse_inbound(
    payload: dict[str, Any], *, phone_number_id: str | None = None
) -> list[InboundAnswer]:
    """Pull button replies and text replies out of a verified webhook payload.

    Only events for this merchant's own phone number are read. A WhatsApp Business Account can
    hold several numbers, and a subscription is made at the account level, so every app subscribed
    to the account receives every number's events. Acting on another number's traffic would mean
    settling escalations from replies sent to somebody else's product.
    """
    expected = phone_number_id if phone_number_id is not None else settings.META_PHONE_NUMBER_ID
    answers: list[InboundAnswer] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            arrived_on = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if expected and arrived_on and arrived_on != expected:
                continue
            for message in value.get("messages", []) or []:
                message_id = message.get("id")
                sender = message.get("from")
                interactive = message.get("interactive") or {}
                reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                # A free-form interactive message returns the button id under "interactive"; a
                # template quick reply returns the send-time payload under "button" instead. Both
                # carry the same value, so both are read here.
                template_reply = (message.get("button") or {}).get("payload", "")
                reply_id = str(reply.get("id", "") or template_reply)
                text = str((message.get("text") or {}).get("body", "")).strip()

                escalation_id: str | None = None
                answer = "unknown"
                if reply_id.startswith(APPROVE_ID):
                    answer = "approve"
                    escalation_id = reply_id.split(":", 1)[1] if ":" in reply_id else None
                elif reply_id.startswith(DENY_ID):
                    answer = "deny"
                    escalation_id = reply_id.split(":", 1)[1] if ":" in reply_id else None
                elif text:
                    lowered = text.lower()
                    parts = lowered.split()
                    if parts and parts[0] in ("approve", "yes", "y"):
                        answer = "approve"
                    elif parts and parts[0] in ("deny", "no", "n"):
                        answer = "deny"
                    if len(parts) > 1:
                        escalation_id = parts[1]
                answers.append(
                    InboundAnswer(escalation_id, answer, message_id, sender, text[:500])
                )
    return answers


class WhatsAppTransport(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class CloudApiTransport:
    """Real Meta Cloud API sender."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not settings.META_ACCESS_TOKEN or not settings.META_PHONE_NUMBER_ID:
            raise RuntimeError(
                "META_ACCESS_TOKEN and META_PHONE_NUMBER_ID must be set to send WhatsApp messages"
            )
        url = (
            f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}"
            f"/{settings.META_PHONE_NUMBER_ID}/messages"
        )
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


class RecordingTransport:
    """Test and offline transport. Records what would have been sent, contacts nothing."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_with = fail_with

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(payload)
        return {"messages": [{"id": f"wamid.recorded.{len(self.sent)}"}]}


def default_transport() -> WhatsAppTransport:
    """The sender used when a caller does not supply one.

    Real delivery in normal operation, and a recording stub under APP_ENV=testing so the suite
    never needs Meta credentials and never makes a network call.
    """
    if settings.APP_ENV == "testing":
        return RecordingTransport()
    return CloudApiTransport()


def _one_line(value: str, limit: int) -> str:
    """Template parameters may not contain newlines or tabs, and are length capped by Meta."""
    return " ".join(str(value).split())[:limit]


def build_approval_template_message(
    *,
    to_number: str,
    escalation_id: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    constraint_text: str,
    template_name: str,
    language_code: str,
) -> dict[str, Any]:
    """The same prompt as an approved template, for use outside the customer service window.

    A business may only send free-form messages within 24 hours of the person's last message.
    Outside it the Cloud API accepts the send and delivers nothing, so an escalation raised from a
    quiet inbox would never reach the human. An approved template always delivers, and its quick
    reply payloads are set per send, which is what carries the escalation id back.
    """
    major = amount_minor / 100
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
                        {"type": "text", "text": f"{currency} {major:.2f}"},
                        {"type": "text", "text": _one_line(cart_summary, 300)},
                        {"type": "text", "text": _one_line(constraint_text, 300)},
                        {"type": "text", "text": _one_line(escalation_id, 64)},
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [
                        {"type": "payload", "payload": f"{APPROVE_ID}:{escalation_id}"}
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "1",
                    "parameters": [{"type": "payload", "payload": f"{DENY_ID}:{escalation_id}"}],
                },
            ],
        },
    }


def build_approval_message(
    *,
    to_number: str,
    escalation_id: str,
    merchant_name: str,
    amount_minor: int,
    currency: str,
    cart_summary: str,
    constraint_text: str,
) -> dict[str, Any]:
    """An interactive approve or deny prompt naming the cart, amount, merchant and constraint."""
    major = amount_minor / 100
    body = (
        f"{merchant_name} is asking an automated purchase to be confirmed.\n\n"
        f"Amount: {currency} {major:.2f}\n"
        f"Cart: {cart_summary}\n\n"
        f"This could not be decided automatically because of your instruction:\n"
        f'"{constraint_text}"\n\n'
        f"Reference: {escalation_id}"
    )
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": f"{APPROVE_ID}:{escalation_id}", "title": "Approve"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": f"{DENY_ID}:{escalation_id}", "title": "Deny"},
                    },
                ]
            },
        },
    }
