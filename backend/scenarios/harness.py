"""The scaffolding every scenario suite is written against.

Three pieces:

    Client      a tiny HTTP client, so the suite adds no dependency the merchant does not have
    Shopper     the Trusted Surface, the Credential Provider and the Shopping Agent, played
                against a live merchant over HTTP
    Suite       a recorder. Every case declares what it proves and what it expects before it
                runs, so a case that quietly stopped proving anything is visible in the report

Cases record rather than assert. A failing case does not stop the run, because the point of the
report is to say what the gate does across everything, not to stop at the first surprise.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness import factory  # noqa: E402
from app.settings import settings  # noqa: E402
from app.trust.registry import publish_key  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8000"

# Every run gets its own agent namespace. Inventory holds and hold quotas are per agent, so a
# rerun that reused an identity would be refused by its own previous run's holds.
RUN_ID = secrets.token_hex(4)


class SuiteError(RuntimeError):
    """The suite cannot proceed, as distinct from a case that failed."""


@dataclass
class Case:
    id: str
    suite: str
    proves: str
    expected: str
    observed: str = ""
    passed: bool = False
    duration_ms: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "suite": self.suite,
            "what_it_proves": self.proves,
            "expected": self.expected,
            "observed": self.observed,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "note": self.note,
        }


@dataclass
class Suite:
    id: str
    title: str
    description: str
    cases: list[Case] = field(default_factory=list)
    skipped: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if not c.passed]

    @property
    def duration_ms(self) -> int:
        return sum(c.duration_ms for c in self.cases)

    @contextmanager
    def case(self, case_id: str, proves: str, expected: str) -> Iterator[Case]:
        """Record one case, timed. An exception inside is a failure, not a crash."""
        entry = Case(id=f"{self.id}.{case_id}", suite=self.id, proves=proves, expected=expected)
        self.cases.append(entry)
        started = time.perf_counter()
        try:
            yield entry
        except Exception as exc:
            entry.passed = False
            entry.observed = entry.observed or f"{type(exc).__name__}: {exc}"
            entry.note = "the case raised rather than returning a result"
        finally:
            entry.duration_ms = int((time.perf_counter() - started) * 1000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "skipped": self.skipped,
            "total": len(self.cases),
            "passed": self.passed,
            "failed": len(self.failed),
            "duration_ms": self.duration_ms,
            "cases": [c.as_dict() for c in self.cases],
        }


def record(case: Case, condition: bool, observed: str, note: str = "") -> bool:
    case.passed = bool(condition)
    case.observed = observed
    if note:
        case.note = note
    return case.passed


class Client:
    """One HTTP client for the whole suite, with the merchant token attached."""

    def __init__(self, base: str, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "ngrok-skip-browser-warning": "1",
                "X-Merchant-Token": settings.MERCHANT_API_TOKEN,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": raw.decode("utf-8", "replace")[:400]}
        except urllib.error.URLError as exc:
            raise SuiteError(f"{method} {path} could not reach the merchant: {exc}") from exc

    def get(self, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: dict[str, Any], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.request("POST", path, body, **kwargs)

    def patch(self, path: str, body: dict[str, Any], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.request("PATCH", path, body, **kwargs)

    def post_signed_webhook(
        self, path: str, body: dict[str, Any], secret: str
    ) -> tuple[int, dict[str, Any]]:
        """Post exactly the bytes a gateway would, signed over those bytes."""
        import hashlib
        import hmac

        raw = json.dumps(body).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            self.base + path,
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "ngrok-skip-browser-warning": "1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            try:
                return exc.code, json.loads(raw_body or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": raw_body.decode("utf-8", "replace")[:300]}


@dataclass
class Quoted:
    checkout_id: str
    checkout_jwt: str
    checkout_hash: str
    policy_hash: str
    amount_minor: int
    currency: str


class Shopper:
    """One buyer: a human's trusted surface, a credential provider and an agent.

    Kept as an object rather than free functions because the open mandates are signed once and
    presented many times, and several suites depend on that distinction. Re-issuing per attempt
    would give each presentation a fresh digest and silently reset every per-mandate counter.
    """

    def __init__(self, client: Client, name: str, *, issuer: str | None = None) -> None:
        self.client = client
        self.agent_id = f"agent:{name}-{RUN_ID}"
        self.issuer_id = issuer or factory.DEFAULT_ISSUER
        self.principals = factory.Principals.create(
            agent_id=self.agent_id, issuer_id=self.issuer_id, register=False
        )
        # The suite is a separate process from the merchant, so an in-memory registration would be
        # invisible. Publishing into the JWKS file the registry already names is what a real
        # issuing authority does, and keeps the trusted set a configuration decision.
        publish_key(self.issuer_id, self.principals.issuer.public_jwk())
        self._nonce = 0

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Agent-Id": self.agent_id}

    def next_nonce(self, label: str = "n") -> str:
        self._nonce += 1
        return f"{self.agent_id}-{label}-{self._nonce}"

    def quote(self, lines: list[tuple[str, int]]) -> tuple[int, dict[str, Any]]:
        return self.client.post(
            "/checkout/quote",
            {"items": [{"sku": sku, "quantity": qty} for sku, qty in lines]},
            headers=self.headers,
        )

    def quoted(self, lines: list[tuple[str, int]]) -> Quoted:
        status, body = self.quote(lines)
        if status != 200:
            raise SuiteError(f"quote refused with HTTP {status}: {body}")
        return Quoted(
            checkout_id=body["checkout_id"],
            checkout_jwt=body["checkout_jwt"],
            checkout_hash=body["checkout_hash"],
            policy_hash=body["policy_hash"],
            amount_minor=body["total"]["amount"],
            currency=body["total"]["currency"],
        )

    def authorise(
        self,
        cart: list[tuple[str, str, int]],
        *,
        amount_cap_minor: int = 5_000_000,
        natural_language: list[str] | None = None,
        tamper: factory.Tamper | None = None,
        **spec_kwargs: Any,
    ) -> factory.IssuedMandates:
        """The human signs the two open mandates, once.

        The tamper belongs here as well as at presentation. Forgery, expiry, an exaggerated clock
        and an unknown authority are all properties of how the mandate was issued, so a suite that
        only tampered at presentation would silently test nothing for those families.
        """
        spec = factory.spec_for_cart(
            cart,
            amount_cap_minor=amount_cap_minor,
            natural_language=natural_language or [],
            **spec_kwargs,
        )
        return factory.issue_open_mandates(self.principals, spec, tamper=tamper)

    def present(
        self,
        issued: factory.IssuedMandates,
        quoted: Quoted,
        *,
        audience: str,
        tamper: factory.Tamper | None = None,
        nonce: str | None = None,
        human_present: bool = False,
    ) -> factory.Presentation:
        return factory.present_issued(
            issued,
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.amount_minor,
            audience=audience,
            nonce=nonce or self.next_nonce(),
            tamper=tamper,
            human_present=human_present,
        )

    def confirm(
        self, escalation_id: str, quoted: Quoted, decision: str
    ) -> tuple[int, dict[str, Any]]:
        """Answer an escalation the way a person at the surface does: with a signature."""
        return self.client.post(
            "/checkout/confirm",
            {
                "escalation_id": escalation_id,
                "confirmation": factory.sign_confirmation(
                    self.principals,
                    escalation_id=escalation_id,
                    checkout_hash=quoted.checkout_hash,
                    decision=decision,
                ),
            },
        )

    def complete(
        self, presentation: factory.Presentation, *, buyer_region: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        credentials = presentation.credentials
        return self.client.post(
            "/checkout/complete",
            {
                "open_checkout_mandate": credentials.open_checkout,
                "closed_checkout_mandate": credentials.closed_checkout,
                "open_payment_mandate": credentials.open_payment,
                "closed_payment_mandate": credentials.closed_payment,
                "nonce": credentials.nonce,
                "presence_attestation": credentials.presence,
                "buyer_region": buyer_region,
            },
        )

    def buy(
        self,
        cart: list[tuple[str, str, int]],
        *,
        audience: str,
        amount_cap_minor: int = 5_000_000,
        natural_language: list[str] | None = None,
        tamper: factory.Tamper | None = None,
        pay: bool = True,
        human_present: bool = False,
        buyer_region: str | None = None,
        **spec_kwargs: Any,
    ) -> dict[str, Any]:
        """Quote, authorise, present and settle. The ordinary path, in one call."""
        quote = self.quoted([(sku, qty) for sku, _title, qty in cart])
        issued = self.authorise(
            cart,
            amount_cap_minor=amount_cap_minor,
            natural_language=natural_language,
            tamper=tamper,
            **spec_kwargs,
        )
        presentation = self.present(
            issued, quote, audience=audience, tamper=tamper, human_present=human_present
        )
        status, outcome = self.complete(presentation, buyer_region=buyer_region)
        outcome["_http_status"] = status
        outcome["_quote"] = quote
        outcome["_issued"] = issued
        outcome["_shopper"] = self
        if pay and outcome.get("status") == "awaiting_payment":
            pay_order(self.client, outcome)
            status, refreshed = self.client.get(f"/checkout/{quote.checkout_id}")
            if status == 200:
                outcome["status"] = (
                    "completed" if refreshed.get("state") == "completed" else outcome["status"]
                )
        return outcome


def pay_order(client: Client, completion: dict[str, Any]) -> bool:
    """Play the mocked Credential Provider by delivering the capture Razorpay would send.

    The Credential Provider is out of scope for this project, so nothing here can create a real
    payment. What it can do is deliver the correctly signed notification, which exercises the real
    signature check and the real settlement path rather than a shortcut.
    """
    detail = completion.get("detail") or {}
    order_id = detail.get("razorpay_order_id")
    if not order_id:
        return False
    amount = (detail.get("amount") or {}).get("amount", 0)
    currency = (detail.get("amount") or {}).get("currency", "INR")
    event = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_suite_{order_id[-14:]}",
                    "order_id": order_id,
                    "amount": amount,
                    "currency": currency,
                    "status": "captured",
                    "captured": True,
                }
            }
        },
    }
    status, applied = client.post_signed_webhook(
        "/webhooks/razorpay", event, settings.RAZORPAY_WEBHOOK_SECRET
    )
    return status == 200 and "checkout.finalised" in (applied.get("handled") or [])


def merchant_audience(client: Client) -> str:
    """The audience the merchant says its key-binding proofs must carry.

    Never the URL the caller dialled: the merchant may sit behind a proxy or a tunnel, and a proof
    addressed to the wrong audience is refused.
    """
    status, document = client.get("/.well-known/ap2-merchant")
    if status != 200:
        raise SuiteError(f"the merchant did not serve a discovery document (HTTP {status})")
    return str(document.get("audience") or client.base)


def reason_of(outcome: dict[str, Any]) -> str:
    """The reason code, wherever this response happened to put it."""
    if "reason_code" in outcome:
        return str(outcome["reason_code"])
    error = outcome.get("error")
    if isinstance(error, dict):
        return str(error.get("code", ""))
    return ""


@dataclass
class Scale:
    """How hard a profile drives each suite."""

    name: str
    purchases: int = 3
    concurrency: int = 8
    agents: int = 4
    structuring_attempts: int = 5
    soak_seconds: int = 0

    @classmethod
    def for_profile(cls, profile: str, *, minutes: float = 0.0, agents: int = 0) -> Scale:
        base = {
            "smoke": cls("smoke", purchases=1, concurrency=4, agents=2, structuring_attempts=3),
            "standard": cls("standard", purchases=3, concurrency=12, agents=5,
                            structuring_attempts=6, soak_seconds=20),
            "demo": cls("demo", purchases=6, concurrency=16, agents=10,
                        structuring_attempts=8, soak_seconds=45),
            "full": cls("full", purchases=10, concurrency=20, agents=12,
                        structuring_attempts=10, soak_seconds=60),
            "soak": cls("soak", purchases=8, concurrency=24, agents=12,
                        structuring_attempts=10, soak_seconds=300),
        }[profile]
        if minutes:
            base.soak_seconds = int(minutes * 60)
        if agents:
            base.agents = agents
        return base


@dataclass
class Context:
    client: Client
    audience: str
    scale: Scale
    base: str
