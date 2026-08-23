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


def parse_inbound(payload: dict[str, Any]) -> list[InboundAnswer]:
    """Pull button replies and text replies out of a verified webhook payload."""
    answers: list[InboundAnswer] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            for message in value.get("messages", []) or []:
                message_id = message.get("id")
                sender = message.get("from")
                interactive = message.get("interactive") or {}
                reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                reply_id = str(reply.get("id", ""))
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
