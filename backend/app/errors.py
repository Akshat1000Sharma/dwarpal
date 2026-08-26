"""Machine-actionable errors returned to agents.

An agent must be able to decide from the response alone whether to retry, present different
credentials, reduce the cart or stop. Prose error strings are not sufficient, so every failure
carries a reason code from the closed set plus the derived action.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.correlation import get_correlation_id
from app.kernel.reasons import AgentAction, ReasonCode, action_for, is_retryable

STATUS_FOR_CODE: dict[ReasonCode, int] = {
    ReasonCode.CRED_MALFORMED: 400,
    ReasonCode.CRED_SCHEMA_INVALID: 400,
    ReasonCode.CRED_SIGNATURE_INVALID: 401,
    ReasonCode.CRED_SUBJECT_MISMATCH: 403,
    ReasonCode.CRED_KEY_BINDING_MISSING: 401,
    ReasonCode.CRED_KEY_BINDING_INVALID: 401,
    ReasonCode.CRED_ISSUER_UNKNOWN: 403,
    ReasonCode.CRED_ISSUER_TIER_INSUFFICIENT: 403,
    ReasonCode.CRED_EXPIRED: 401,
    ReasonCode.CRED_NOT_YET_VALID: 401,
    ReasonCode.CRED_REPLAYED: 409,
    ReasonCode.CHECKOUT_UNKNOWN: 404,
    ReasonCode.CHECKOUT_EXPIRED: 409,
    ReasonCode.CHECKOUT_ALREADY_SETTLED: 409,
    ReasonCode.CHECKOUT_BINDING_MISMATCH: 409,
    ReasonCode.CART_ALTERED_AFTER_SIGNING: 409,
    ReasonCode.PRICE_DRIFT: 409,
    ReasonCode.POLICY_HASH_MISMATCH: 409,
    ReasonCode.INVENTORY_UNAVAILABLE: 409,
    ReasonCode.HOLD_QUOTA_EXCEEDED: 429,
    ReasonCode.HOLD_EXPIRED: 409,
    ReasonCode.ITEM_UNKNOWN: 404,
    ReasonCode.MANDATE_UNKNOWN: 404,
    ReasonCode.VELOCITY_SPEND_EXCEEDED: 429,
    ReasonCode.VELOCITY_COUNT_EXCEEDED: 429,
    ReasonCode.ESCALATION_REQUIRED: 202,
    # The degraded path answers with the payment-required status and a challenge
    # naming exactly which credentials would unlock the attempt.
    ReasonCode.UNVERIFIED_CEILING_EXCEEDED: 402,
    ReasonCode.UNVERIFIED_CATEGORY_FORBIDDEN: 402,
    ReasonCode.PAYMENT_GATEWAY_ERROR: 502,
    ReasonCode.WEBHOOK_SIGNATURE_INVALID: 401,
}

DEFAULT_STATUS = 403


def status_for(code: ReasonCode) -> int:
    return STATUS_FOR_CODE.get(code, DEFAULT_STATUS)


class AgentError(Exception):
    """A refusal that an agent can act on programmatically."""

    def __init__(
        self,
        code: ReasonCode,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.challenge = challenge
        self.status_code = status_code or status_for(code)

    @property
    def action(self) -> AgentAction:
        return action_for(self.code)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "action": self.action.value,
                "retryable": is_retryable(self.code),
                "detail": self.detail,
                "correlation_id": get_correlation_id(),
            }
        }
        if self.challenge is not None:
            payload["error"]["challenge"] = self.challenge
        return payload


def agent_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AgentError)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(),
        headers={"X-Correlation-Id": get_correlation_id()},
    )


def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to an agent, but still give it something it can act on.
    del exc
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "The merchant endpoint failed to process the request.",
                "action": AgentAction.RETRY.value,
                "retryable": True,
                "detail": {},
                "correlation_id": get_correlation_id(),
            }
        },
        headers={"X-Correlation-Id": get_correlation_id()},
    )
