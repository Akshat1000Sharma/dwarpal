"""Preflight for the outbound channels.

A misconfigured messaging channel does not fail loudly. Meta accepts a send and answers with an
error only for that one message, so a template that was configured but never created shows up as a
delivery error on the first escalation that needed it, hours after anyone was watching. This is the
check that turns that into an answer you can get on demand.

It sends nothing. Every call here is a read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.settings import settings

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, fix: str = "") -> None:
        self.checks.append(Check(name, ok, detail, fix))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failures,
            "checked": len(self.checks),
            "failed": len(self.failures),
            "checks": [c.as_dict() for c in self.checks],
        }


def _redact(text: str, token: str) -> str:
    """Whatever this text is about to be printed into, it is not going to contain the token."""
    return text.replace(token, "<redacted>") if token else text


def _get(url: str, token: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Probe one Graph endpoint.

    The token goes in the Authorization header, never the query string: a URL is logged by every
    intermediary that touches it and is echoed back inside httpx's own exception text, which this
    function returns into a report the CLI prints.
    """
    import httpx

    try:
        response = httpx.get(
            url,
            params=dict(params or {}),
            headers={"Authorization": f"Bearer {token}"},
            timeout=25,
        )
    except Exception as exc:
        return 0, {"error": {"message": _redact(f"{type(exc).__name__}: {exc}", token)}}
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"message": _redact(response.text[:200], token)}}
    return response.status_code, body if isinstance(body, dict) else {"data": body}


def check_razorpay(report: Report) -> None:
    key = settings.RAZORPAY_KEY_ID
    report.add(
        "razorpay key is test mode",
        key.startswith("rzp_test_"),
        f"key id {key[:14]}...",
        "Dwarpal refuses to start against a live key; this should never fail here.",
    )


def check_whatsapp(report: Report) -> None:
    version = settings.META_GRAPH_VERSION
    token = settings.META_ACCESS_TOKEN

    recipient = settings.ESCALATION_HUMAN_WHATSAPP
    report.add(
        "escalation recipient is E.164",
        bool(recipient) and bool(_E164.match(recipient)),
        recipient or "(unset)",
        "Set ESCALATION_HUMAN_WHATSAPP to a number like +919876543210.",
    )

    if not token or not settings.META_PHONE_NUMBER_ID:
        report.add(
            "whatsapp credentials present",
            False,
            "META_ACCESS_TOKEN or META_PHONE_NUMBER_ID is unset",
            "Outbound messaging is disabled until both are set.",
        )
        return
    report.add("whatsapp credentials present", True, "token and phone number id are set")

    status, body = _get(f"https://graph.facebook.com/{version}/debug_token", token,
                        {"input_token": token})
    data = body.get("data", {})
    report.add(
        "access token is valid",
        status == 200 and bool(data.get("is_valid")),
        f"type {data.get('type', 'unknown')}, scopes {','.join(data.get('scopes', []) or [])}"
        if status == 200
        else str(body.get("error", {}).get("message", ""))[:120],
        "Regenerate the System User token under Business Settings.",
    )

    status, body = _get(
        f"https://graph.facebook.com/{version}/{settings.META_PHONE_NUMBER_ID}", token,
        {"fields": "display_phone_number,verified_name,quality_rating,webhook_configuration"},
    )
    report.add(
        "phone number is reachable",
        status == 200,
        f"{body.get('display_phone_number', '')} ({body.get('verified_name', '')}), "
        f"quality {body.get('quality_rating', '?')}"
        if status == 200
        else str(body.get("error", {}).get("message", ""))[:120],
        "Check META_PHONE_NUMBER_ID against WhatsApp, API Setup.",
    )
    if status == 200:
        configured = (body.get("webhook_configuration") or {}).get("application")
        expected = settings.PUBLIC_BASE_URL.rstrip("/") + "/webhooks/whatsapp"
        report.add(
            "webhook points back here",
            configured == expected,
            f"configured {configured or '(none)'}",
            f"Meta will deliver replies to {configured or 'nowhere'}, not {expected}. "
            "Update the callback URL, or PUBLIC_BASE_URL.",
        )

    _check_shared_account(report, version, token)
    _check_templates(report, version, token)


def _check_shared_account(report: Report, version: str, token: str) -> None:
    """Say who else is listening, because a subscription is per account, not per app.

    Every app subscribed to a WhatsApp Business Account receives every event for every number on
    it. That is how a button tapped on this merchant's number ends up answered by somebody else's
    product from a different number. Dwarpal ignores traffic for numbers that are not its own, so
    this cannot corrupt a decision here, but the operator should know the account is shared.
    """
    if not settings.META_WABA_ID:
        return

    status, body = _get(
        f"https://graph.facebook.com/{version}/{settings.META_WABA_ID}/subscribed_apps", token
    )
    if status != 200:
        return
    apps = [
        entry.get("whatsapp_business_api_data", {}) for entry in body.get("data", []) or []
    ]
    others = [a for a in apps if str(a.get("id")) != settings.META_APP_ID]
    report.add(
        "this account is not shared with other apps",
        not others,
        f"{len(apps)} app(s) subscribed"
        + (f", {len(others)} of them not this one, each receiving every event"
           if others else ""),
        "Dwarpal ignores events for numbers that are not its own, so decisions here are safe. "
        "Those apps still see this merchant's traffic and may reply to it from their own numbers. "
        "Unsubscribe them from this account, or give Dwarpal an account of its own.",
    )

    status, body = _get(
        f"https://graph.facebook.com/{version}/{settings.META_WABA_ID}/phone_numbers", token,
        {"limit": 50},
    )
    if status != 200:
        return
    numbers = body.get("data", []) or []
    mine = [n for n in numbers if n.get("id") == settings.META_PHONE_NUMBER_ID]
    report.add(
        "the configured number belongs to this account",
        bool(mine),
        (f"{mine[0].get('display_phone_number')} ({mine[0].get('verified_name')}), "
         f"{len(numbers)} number(s) on the account")
        if mine
        else f"{settings.META_PHONE_NUMBER_ID} is not one of this account's {len(numbers)} numbers",
        "Templates must be approved on the account the sending number belongs to. If these "
        "disagree, META_WABA_ID and META_PHONE_NUMBER_ID are pointing at different accounts.",
    )


def _check_templates(report: Report, version: str, token: str) -> None:
    """Confirm the templates named in configuration actually exist, in the language named.

    Without a WABA id this cannot be answered, and the check says so rather than passing silently:
    a check that quietly skips the thing it was written for is worse than no check.
    """
    wanted = [
        ("META_TEMPLATE_NAME", settings.META_TEMPLATE_NAME, settings.META_TEMPLATE_LANGUAGE),
        (
            "META_RECEIPT_TEMPLATE_NAME",
            settings.META_RECEIPT_TEMPLATE_NAME,
            settings.META_RECEIPT_TEMPLATE_LANGUAGE,
        ),
    ]
    configured = [(k, n, lang) for k, n, lang in wanted if n]
    if not configured:
        report.add(
            "message templates",
            True,
            "none configured; every send uses the free-form message",
            "Free-form only delivers inside the 24 hour customer service window.",
        )
        return

    if not settings.META_WABA_ID:
        report.add(
            "message templates",
            False,
            f"{len(configured)} template(s) configured but META_WABA_ID is unset, so their "
            "existence cannot be checked",
            "Set META_WABA_ID to the WhatsApp Business Account id. It appears as entry[0].id in "
            "any inbound webhook payload.",
        )
        return

    status, body = _get(
        f"https://graph.facebook.com/{version}/{settings.META_WABA_ID}/message_templates",
        token,
        {"limit": 200},
    )
    if status != 200:
        report.add(
            "message templates",
            False,
            f"could not list templates: {str(body.get('error', {}).get('message', ''))[:110]}",
            "The token needs whatsapp_business_management on that WABA.",
        )
        return

    available = {
        (t.get("name"), t.get("language")): t.get("status") for t in body.get("data", [])
    }
    for key, name, language in configured:
        state = available.get((name, language))
        elsewhere = sorted({lang for (n, lang) in available if n == name})
        if state == "APPROVED":
            detail = f"{name} ({language}) is APPROVED"
            fix = ""
        elif state:
            detail = f"{name} ({language}) exists but is {state}"
            fix = "It will not send until Meta approves it."
        elif elsewhere:
            detail = f"{name} does not exist in {language}; it exists in {', '.join(elsewhere)}"
            fix = f"Set the language for {key} to one of those."
        else:
            detail = f"{name} does not exist at all"
            fix = (
                f"Create it, or clear {key} so escalations use the free-form message, which only "
                "delivers inside the 24 hour customer service window."
            )
        report.add(f"template {key}", state == "APPROVED", detail, fix)


def run() -> Report:
    report = Report()
    check_razorpay(report)
    check_whatsapp(report)
    return report
