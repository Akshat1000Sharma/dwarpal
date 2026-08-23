"""Razorpay gateway, and the stub the tests use.

Tests must never require real Razorpay credentials, so everything the application does goes
through the ``PaymentGateway`` protocol and the stub implements the same contract, including its
failure modes.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.logging import get_logger
from app.settings import settings

logger = get_logger(__name__)

WEBHOOK_SIGNATURE_HEADER = "X-Razorpay-Signature"


class GatewayError(RuntimeError):
    """Any failure reaching or being refused by the gateway."""


def verify_webhook_signature(
    raw_body: bytes, header_value: str | None, secret: str | None = None
) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    Razorpay signs the exact bytes sent. Parsing the body and re-serialising it would change them,
    so the signature is always checked before anything else touches the payload.
    """
    if not header_value:
        return False
    key = secret if secret is not None else settings.RAZORPAY_WEBHOOK_SECRET
    if not key:
        return False
    expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.strip())


class PaymentGateway(Protocol):
    def create_order(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> dict[str, Any]: ...

    def fetch_order(self, order_id: str) -> dict[str, Any]: ...

    def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    def capture_payment(
        self, payment_id: str, amount_minor: int, currency: str
    ) -> dict[str, Any]: ...

    def create_refund(
        self, payment_id: str, *, amount_minor: int, notes: dict[str, str]
    ) -> dict[str, Any]: ...

    def fetch_refund(self, refund_id: str) -> dict[str, Any]: ...


class RazorpayGateway:
    """Real calls against Razorpay test mode, over the documented REST API.

    Called directly rather than through the razorpay SDK: that package still imports
    ``pkg_resources``, which no longer ships with Python 3.12 and later, and it is a thin wrapper
    over these same endpoints. Basic auth with the key pair is exactly what the SDK sends.
    """

    BASE_URL = "https://api.razorpay.com/v1"

    def __init__(
        self, key_id: str | None = None, key_secret: str | None = None, timeout: float = 30.0
    ) -> None:
        self.key_id = key_id or settings.RAZORPAY_KEY_ID
        self.key_secret = key_secret or settings.RAZORPAY_KEY_SECRET
        self.timeout = timeout

    def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import httpx

        try:
            response = httpx.request(
                method,
                f"{self.BASE_URL}{path}",
                json=payload,
                auth=(self.key_id, self.key_secret),
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            raise GatewayError(f"{method} {path} could not reach Razorpay: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:400]
            raise GatewayError(f"{method} {path} returned HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError(f"{method} {path} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise GatewayError(
                f"{method} {path} returned {type(body).__name__}, expected an object"
            )
        return body

    def create_order(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/orders",
            {
                "amount": amount_minor,
                "currency": currency,
                # Razorpay caps the receipt field at 40 characters.
                "receipt": receipt[:40],
                "notes": notes,
                # Capture explicitly, so no money moves without Dwarpal deciding it should.
                "payment_capture": 0,
            },
        )

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return self._call("GET", f"/orders/{order_id}")

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._call("GET", f"/payments/{payment_id}")

    def capture_payment(
        self, payment_id: str, amount_minor: int, currency: str
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/payments/{payment_id}/capture",
            {"amount": amount_minor, "currency": currency},
        )

    def create_refund(
        self, payment_id: str, *, amount_minor: int, notes: dict[str, str]
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/payments/{payment_id}/refund",
            {"amount": amount_minor, "speed": "normal", "notes": notes},
        )

    def fetch_refund(self, refund_id: str) -> dict[str, Any]:
        return self._call("GET", f"/refunds/{refund_id}")


@dataclass
class StubGateway:
    """Deterministic in-memory gateway used by tests and the offline corpus.

    It reproduces the shapes Razorpay returns, and can be told to fail so the failure paths are
    exercised without a network.
    """

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    refunds: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_on: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _guard(self, operation: str) -> None:
        if operation in self.fail_on:
            raise GatewayError(f"stub gateway configured to fail on {operation}")

    def create_order(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> dict[str, Any]:
        self._guard("create_order")
        order_id = f"order_stub{uuid.uuid4().hex[:14]}"
        order = {
            "id": order_id,
            "entity": "order",
            "amount": amount_minor,
            "amount_paid": 0,
            "amount_due": amount_minor,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
            "attempts": 0,
            "notes": dict(notes),
        }
        self.orders[order_id] = order
        self.calls.append(("create_order", order))
        return order

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        self._guard("fetch_order")
        if order_id not in self.orders:
            raise GatewayError(f"unknown order {order_id}")
        return dict(self.orders[order_id])

    def authorize(self, order_id: str, *, amount_minor: int | None = None) -> dict[str, Any]:
        """Stands in for the mocked Credential Provider paying the order in test mode."""
        order = self.orders[order_id]
        payment_id = f"pay_stub{uuid.uuid4().hex[:14]}"
        payment = {
            "id": payment_id,
            "entity": "payment",
            "amount": amount_minor if amount_minor is not None else order["amount"],
            "currency": order["currency"],
            "status": "authorized",
            "order_id": order_id,
            "captured": False,
            "method": "card",
            "amount_refunded": 0,
        }
        self.payments[payment_id] = payment
        order["attempts"] = int(order["attempts"]) + 1
        return dict(payment)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        self._guard("fetch_payment")
        if payment_id not in self.payments:
            raise GatewayError(f"unknown payment {payment_id}")
        return dict(self.payments[payment_id])

    def capture_payment(self, payment_id: str, amount_minor: int, currency: str) -> dict[str, Any]:
        self._guard("capture_payment")
        payment = self.payments.get(payment_id)
        if payment is None:
            raise GatewayError(f"unknown payment {payment_id}")
        # Razorpay treats a capture of an already captured payment as an error, and Dwarpal must
        # not depend on it being idempotent at the gateway.
        if payment["captured"]:
            raise GatewayError("payment has already been captured")
        payment["captured"] = True
        payment["status"] = "captured"
        order = self.orders.get(str(payment["order_id"]))
        if order is not None:
            order["status"] = "paid"
            order["amount_paid"] = amount_minor
            order["amount_due"] = 0
        self.calls.append(("capture_payment", dict(payment)))
        return dict(payment)

    def create_refund(
        self, payment_id: str, *, amount_minor: int, notes: dict[str, str]
    ) -> dict[str, Any]:
        self._guard("create_refund")
        payment = self.payments.get(payment_id)
        if payment is None:
            raise GatewayError(f"unknown payment {payment_id}")
        refund_id = f"rfnd_stub{uuid.uuid4().hex[:13]}"
        refund = {
            "id": refund_id,
            "entity": "refund",
            "amount": amount_minor,
            "currency": payment["currency"],
            "payment_id": payment_id,
            "status": "processed",
            "speed_processed": "normal",
            "notes": dict(notes),
        }
        self.refunds[refund_id] = refund
        payment["amount_refunded"] = int(payment.get("amount_refunded", 0)) + amount_minor
        if payment["amount_refunded"] >= payment["amount"]:
            payment["status"] = "refunded"
        self.calls.append(("create_refund", refund))
        return refund

    def fetch_refund(self, refund_id: str) -> dict[str, Any]:
        self._guard("fetch_refund")
        if refund_id not in self.refunds:
            raise GatewayError(f"unknown refund {refund_id}")
        return dict(self.refunds[refund_id])


_gateway: PaymentGateway | None = None


def get_gateway() -> PaymentGateway:
    global _gateway
    if _gateway is None:
        _gateway = RazorpayGateway()
    return _gateway


def set_gateway(gateway: PaymentGateway | None) -> None:
    """Used by tests and the offline corpus to install the stub."""
    global _gateway
    _gateway = gateway
