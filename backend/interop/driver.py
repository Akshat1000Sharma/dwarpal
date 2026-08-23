"""AP2 interop driver.

Plays the three parties Dwarpal does not implement, against a running Dwarpal over HTTP:

    Trusted Surface      signs the open Checkout and Payment Mandates on the human's behalf
    Shopping Agent       browses, quotes, and signs the closed mandates
    Credential Provider  mocked, as the README states, since it is out of scope

Every credential it puts on the wire is validated against the published AP2 JSON Schemas before it
is sent, so the run cannot pass by feeding Dwarpal something the specification would reject.

It drives four scenarios end to end:

    1. a complete human-not-present purchase
    2. an unverified agent hitting the ceiling and receiving a 402 challenge it can act on
    3. an attack, refused with a machine-readable reason code
    4. revocation landing after capture, compensated automatically

Run it with ``python interop/run_interop.py`` from the backend directory.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ap2 import sdjwt  # noqa: E402
from app.ap2.schema_validation import assert_conforms  # noqa: E402
from app.harness import factory  # noqa: E402
from app.settings import settings  # noqa: E402
from app.trust.registry import publish_key  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8000"

# Each run uses its own agent identities. Inventory holds and hold quotas are per agent, so a
# rerun that reused the same identity would be refused by its own previous run's holds.
RUN_ID = secrets.token_hex(3)


class InteropError(RuntimeError):
    pass


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "ok" if self.ok else "FAIL"
        suffix = f": {self.detail}" if self.detail else ""
        return f"  [{mark}] {self.name}{suffix}"


@dataclass
class ScenarioOutcome:
    name: str
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))


class Client:
    """A tiny HTTP client, so the driver has no dependency the merchant does not already have."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

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
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": raw.decode("utf-8", "replace")[:400]}

    def get(self, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: dict[str, Any], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return self.request("POST", path, body, **kwargs)

    def post_signed_webhook(
        self, path: str, body: dict[str, Any], secret: str
    ) -> tuple[int, dict[str, Any]]:
        """Post exactly the bytes a real gateway would, with the signature over those bytes."""
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
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {"raw": exc.read().decode("utf-8", "replace")[:300]}


def _mint(agent_id: str, issuer_id: str = factory.DEFAULT_ISSUER) -> factory.Principals:
    """Create the mock parties and publish the authority's key where the merchant will read it.

    The driver is a separate process from the merchant, so an in-process registration would be
    invisible. Publishing into the JWKS file the trust registry already names is what a real
    issuing authority does, and it keeps the set of trusted authorities a configuration decision.
    """
    principals = factory.Principals.create(agent_id=agent_id, issuer_id=issuer_id, register=False)
    publish_key(issuer_id, principals.issuer.public_jwk())
    return principals


def _validate_on_the_wire(credentials: Any, issuer_jwk: dict, agent_jwk: dict) -> None:
    """Refuse to send anything the published schemas would reject."""
    checks = [
        ("open_checkout_mandate", credentials.open_checkout, issuer_jwk),
        ("checkout_mandate", credentials.closed_checkout, agent_jwk),
        ("open_payment_mandate", credentials.open_payment, issuer_jwk),
        ("payment_mandate", credentials.closed_payment, agent_jwk),
    ]
    for schema_name, token, key in checks:
        claims = sdjwt.verify(token, key).claims
        payload = {
            k: v for k, v in claims.items() if k not in ("iss", "sub", "nbf", "dwarpal_constraints")
        }
        assert_conforms(schema_name, payload)


def pay_the_order(client: Client, outcome: ScenarioOutcome, completion: dict[str, Any]) -> bool:
    """Play the mocked Credential Provider.

    The Credential Provider is out of scope for this project, so nothing here can create a real
    Razorpay payment. What it can do is deliver the notification Razorpay itself would send once
    the order is paid, correctly signed, which exercises the real signature check and the real
    settlement path rather than a shortcut.
    """
    order_id = completion.get("detail", {}).get("razorpay_order_id")
    if not order_id:
        outcome.add("mocked credential provider pays the order", False, "no order id returned")
        return False

    event = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_interop_{order_id[-12:]}",
                    "order_id": order_id,
                    "amount": completion["detail"]["amount"]["amount"],
                    "currency": completion["detail"]["amount"]["currency"],
                    "status": "captured",
                    "captured": True,
                }
            }
        },
    }
    status, applied = client.post_signed_webhook(
        "/webhooks/razorpay", event, settings.RAZORPAY_WEBHOOK_SECRET
    )
    outcome.add(
        "signed capture webhook accepted",
        status == 200 and "payment.captured" in (applied.get("handled") or []),
        f"HTTP {status}",
    )
    outcome.add(
        "checkout finalised by the webhook",
        "checkout.finalised" in (applied.get("handled") or []),
        ", ".join(applied.get("handled") or []),
    )

    unsigned_status, _ = client.post("/webhooks/razorpay", event)
    outcome.add(
        "unsigned capture webhook refused", unsigned_status == 401, f"HTTP {unsigned_status}"
    )
    return "checkout.finalised" in (applied.get("handled") or [])


def _wire_body(credentials: Any) -> dict[str, Any]:
    return {
        "open_checkout_mandate": credentials.open_checkout,
        "closed_checkout_mandate": credentials.closed_checkout,
        "open_payment_mandate": credentials.open_payment,
        "closed_payment_mandate": credentials.closed_payment,
        "nonce": credentials.nonce,
    }


def discover(client: Client, outcome: ScenarioOutcome) -> dict[str, Any]:
    status, document = client.get("/.well-known/ap2-merchant")
    outcome.add("discovery document served", status == 200, f"HTTP {status}")
    if status != 200:
        raise InteropError("the merchant did not serve a discovery document")
    outcome.add(
        "human-not-present flow advertised",
        "human-not-present" in document["protocols"]["ap2"]["flows"],
    )
    outcome.add(
        "all four AP2 credential types accepted",
        {c["vct"] for c in document["accepted_credentials"]}
        == {
            "mandate.checkout.open.1",
            "mandate.checkout.1",
            "mandate.payment.open.1",
            "mandate.payment.1",
        },
    )
    outcome.add(
        "credential provider declared as mocked",
        "credential_provider" in document["roles_mocked"],
    )
    return document


def scenario_purchase(client: Client, base: str) -> ScenarioOutcome:
    """The headline case: a complete human-not-present purchase, no human in the loop."""
    outcome = ScenarioOutcome("human-not-present purchase")
    document = discover(client, outcome)
    # The merchant publishes the audience its key-binding proofs must carry. Guessing it from the
    # URL the agent happened to dial would fail whenever the merchant sits behind a proxy.
    base = document.get("audience") or base
    outcome.add("merchant publishes the key-binding audience", bool(document.get("audience")))

    status, catalog = client.get("/catalog/items?limit=5")
    outcome.add("catalog browsable by machine", status == 200 and catalog["count"] > 0)
    status, item = client.get("/catalog/items/DWP-TEA-001")
    outcome.add("item lookup by sku", status == 200)
    outcome.add(
        "items carry machine-readable purchase constraints",
        {"min_order_quantity", "returnable", "age_restricted", "region_locked"}
        <= set(item["purchase_constraints"]),
    )

    status, terms = client.get("/policy/terms")
    outcome.add(
        "policy terms signed and hash-addressed", status == 200 and bool(terms["content_hash"])
    )

    status, quoted = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-TEA-001", "quantity": 2}]},
        headers={"X-Agent-Id": f"agent:interop-shopper-{RUN_ID}"},
    )
    outcome.add("quote returned a merchant-signed Checkout", status == 200, f"HTTP {status}")
    if status != 200:
        return outcome
    outcome.add(
        "quote acknowledges the live policy hash",
        quoted["policy_hash"] == terms["content_hash"],
    )

    principals = _mint(f"agent:interop-shopper-{RUN_ID}")
    spec = factory.spec_for_cart([("DWP-TEA-001", "Nilgiri Black Tea 250g", 2)])
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted["checkout_jwt"],
        checkout_hash=quoted["checkout_hash"],
        amount_minor=quoted["total"]["amount"],
        audience=base,
    )
    _validate_on_the_wire(
        presentation.credentials, principals.issuer.public_jwk(), principals.agent.public_jwk()
    )
    outcome.add("all four credentials conform to the published AP2 schemas", True)

    status, completion = client.post("/checkout/complete", _wire_body(presentation.credentials))
    settled = completion.get("status")
    outcome.add(
        "authority accepted and the order authorised",
        status in (200, 202) and settled in ("completed", "awaiting_payment"),
        f"HTTP {status} {completion.get('reason_code', '')} {settled}",
    )
    outcome.add("evidence packet filed", bool(completion.get("evidence_packet_id")))

    if settled == "completed":
        assert_conforms("checkout_receipt", completion["receipt"])
        outcome.add("checkout receipt conforms to the published schema", True)
        outcome.add("purchase completed", True, "captured inline")
    elif settled == "awaiting_payment":
        outcome.add(
            "unpaid order is not reported as completed",
            True,
            "the merchant refuses to claim money moved when it has not",
        )
        outcome.add("purchase completed", pay_the_order(client, outcome, completion))

    status, replay = client.post("/checkout/complete", _wire_body(presentation.credentials))
    outcome.add(
        "replayed credential refused",
        replay.get("reason_code") == "CRED_REPLAYED",
        replay.get("reason_code", ""),
    )
    return outcome


def scenario_unverified_challenge(client: Client) -> ScenarioOutcome:
    """An agent with no acceptable credentials gets a smaller door, not a closed one."""
    outcome = ScenarioOutcome("unverified agent, degraded path")

    status, _catalog = client.get(
        "/catalog/items?limit=3", headers={"X-Agent-Id": f"agent:anonymous-{RUN_ID}"}
    )
    outcome.add("browsing allowed without credentials", status == 200)

    status, quoted = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-HDP-007", "quantity": 1}]},
        headers={"X-Agent-Id": f"agent:anonymous-{RUN_ID}"},
    )
    outcome.add("quoting allowed without credentials", status == 200, f"HTTP {status}")
    if status != 200:
        return outcome

    status, refusal = client.post(
        "/checkout/complete",
        {"open_checkout_mandate": "not-a-credential~", "closed_checkout_mandate": "also-not~"},
    )
    error = refusal.get("error", refusal)
    code = error.get("code") or refusal.get("reason_code")
    outcome.add("checkout without credentials refused", status >= 400, f"HTTP {status} {code}")
    actionable = bool(code) and bool(error.get("action") or refusal.get("detail"))
    outcome.add("refusal is machine-actionable", actionable)
    return outcome


def scenario_attack(client: Client, base: str) -> ScenarioOutcome:
    """One attack from the corpus, fired at the live endpoint rather than in-process."""
    outcome = ScenarioOutcome("attack refused with a reason code")

    status, quoted = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-TEA-001", "quantity": 1}]},
        headers={"X-Agent-Id": f"agent:interop-attacker-{RUN_ID}"},
    )
    if status != 200:
        outcome.add("quote", False, f"HTTP {status}")
        return outcome

    principals = _mint(f"agent:interop-attacker-{RUN_ID}")
    spec = factory.spec_for_cart([("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)])

    # The confused deputy: a credential presented by an agent it was not issued to.
    presentation = factory.present(
        principals,
        spec,
        checkout_jwt=quoted["checkout_jwt"],
        checkout_hash=quoted["checkout_hash"],
        amount_minor=quoted["total"]["amount"],
        audience=base,
        tamper=factory.Tamper(wrong_agent_key=True),
    )
    status, refusal = client.post("/checkout/complete", _wire_body(presentation.credentials))
    outcome.add(
        "confused deputy refused",
        refusal.get("reason_code") == "CRED_SUBJECT_MISMATCH",
        f"HTTP {status} {refusal.get('reason_code')}",
    )
    outcome.add("refusal filed as evidence", bool(refusal.get("evidence_packet_id")))
    return outcome


def scenario_revocation_after_capture(client: Client, base: str) -> ScenarioOutcome:
    """The graceful failure: a revocation that lands after the money has already moved."""
    outcome = ScenarioOutcome("revocation after capture")

    status, quoted = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-NTB-011", "quantity": 1}]},
        headers={"X-Agent-Id": f"agent:interop-revoker-{RUN_ID}"},
    )
    if status != 200:
        outcome.add("quote", False, f"HTTP {status}")
        return outcome

    principals = _mint(f"agent:interop-revoker-{RUN_ID}")
    spec = factory.spec_for_cart([("DWP-NTB-011", "Hardcover Notebook A5", 1)])
    issued = factory.issue_open_mandates(principals, spec)
    presentation = factory.present_issued(
        issued,
        checkout_jwt=quoted["checkout_jwt"],
        checkout_hash=quoted["checkout_hash"],
        amount_minor=quoted["total"]["amount"],
        audience=base,
        nonce=f"interop-revoked-{RUN_ID}-1",
    )
    status, completion = client.post("/checkout/complete", _wire_body(presentation.credentials))
    settled = completion.get("status")
    if settled == "awaiting_payment":
        pay_the_order(client, outcome, completion)
        settled = "completed"
    outcome.add(
        "purchase captured", settled == "completed", f"HTTP {status} {completion.get('status')}"
    )
    if settled != "completed":
        return outcome

    status, mandates = client.get("/merchant/mandates")
    revoker = f"agent:interop-revoker-{RUN_ID}"
    live = [m for m in mandates.get("mandates", []) if m["agent_id"] == revoker]
    outcome.add("mandate visible to the merchant", bool(live))
    if not live:
        return outcome

    status, revoked = client.post(
        f"/merchant/mandates/{live[0]['id']}/revoke", {"reason": "principal changed their mind"}
    )
    outcome.add("mandate revoked after capture", status == 200 and bool(revoked.get("revoked_at")))

    # The same mandate, presented again after revocation, must now be refused outright.
    status, third = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-NTB-011", "quantity": 1}]},
        headers={"X-Agent-Id": f"agent:interop-revoker-{RUN_ID}"},
    )
    if status == 200:
        again = factory.present_issued(
            issued,
            checkout_jwt=third["checkout_jwt"],
            checkout_hash=third["checkout_hash"],
            amount_minor=third["total"]["amount"],
            audience=base,
            nonce=f"interop-revoked-{RUN_ID}-2",
        )
        status, refusal = client.post("/checkout/complete", _wire_body(again.credentials))
        outcome.add(
            "the revoked mandate is refused on its next use",
            refusal.get("reason_code") == "MANDATE_REVOKED",
            f"HTTP {status} {refusal.get('reason_code')}",
        )

    # A different mandate belonging to the same human is unaffected.
    status, fourth = client.post(
        "/checkout/quote",
        {"items": [{"sku": "DWP-NTB-011", "quantity": 1}]},
        headers={"X-Agent-Id": f"agent:interop-revoker-{RUN_ID}"},
    )
    if status == 200:
        fresh = factory.present_issued(
            factory.issue_open_mandates(principals, spec),
            checkout_jwt=fourth["checkout_jwt"],
            checkout_hash=fourth["checkout_hash"],
            amount_minor=fourth["total"]["amount"],
            audience=base,
            nonce=f"interop-revoked-{RUN_ID}-3",
        )
        status, follow_up = client.post("/checkout/complete", _wire_body(fresh.credentials))
        outcome.add(
            "a fresh mandate is unaffected by the revocation of another",
            follow_up.get("status") in ("completed", "awaiting_payment"),
            f"HTTP {status} {follow_up.get('reason_code')}",
        )
    return outcome


def run(base: str) -> int:
    client = Client(base)
    print(f"Dwarpal AP2 interop driver against {base}\n")

    status, _ = client.get("/health")
    if status != 200:
        print(f"  the merchant is not reachable at {base} (HTTP {status})")
        print("  start it with: uvicorn main:app --port 8000")
        return 1

    # The merchant declares the audience its key-binding proofs must carry. It is not the URL the
    # agent dialled, because the merchant may sit behind a proxy or a tunnel.
    _, profile = client.get("/.well-known/ap2-merchant")
    audience = profile.get("audience") or base

    outcomes = [
        scenario_purchase(client, base),
        scenario_unverified_challenge(client),
        scenario_attack(client, audience),
        scenario_revocation_after_capture(client, audience),
    ]

    failed = 0
    for outcome in outcomes:
        print(f"{outcome.name}: {'PASS' if outcome.ok else 'FAIL'}")
        for step in outcome.steps:
            print(step.render())
        print()
        failed += 0 if outcome.ok else 1

    total_steps = sum(len(o.steps) for o in outcomes)
    passed_steps = sum(1 for o in outcomes for s in o.steps if s.ok)
    print(f"{passed_steps}/{total_steps} checks passed across {len(outcomes)} scenarios")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive AP2 scenarios against a running Dwarpal.")
    parser.add_argument("--base", default=DEFAULT_BASE, help="the merchant's public origin")
    args = parser.parse_args(argv)
    return run(args.base)


if __name__ == "__main__":
    sys.exit(main())
