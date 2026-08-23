"""The deterministic policy kernel.

Nothing in this package may import a model client. The separation is the enforcement mechanism for
the invariant that no model touches a money decision, and ``tests/test_kernel_isolation.py`` walks
the transitive import closure of every ``app.kernel.*`` module to prove it. Do not weaken it for
convenience.

Evaluation order is fixed and documented. The kernel refuses at the first failure, and its
refusals are final: the semantic check is consulted only on constraints this module could not
evaluate, and only when this module has not already refused.

    1.  agent kill switch
    2.  mandate revocation
    3.  acknowledged policy hash matches the hash live at quote time
    4.  per-item policy: quantity range, age restriction, region lock, restricted category
    5.  trust-tier ceiling, which is where an unverified agent receives a challenge
    6.  deterministic constraint satisfaction, closed mandate against open
    7.  merchant-set agent controls: category gates and per-window spend and count
    8.  structuring across the rolling window
    9.  budget reservation against the open mandate cap, under a row lock
    10. anything the kernel could not decide becomes an escalation, never an approval
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.ap2.constraints import ConstraintResult, first_violation, unresolved
from app.db.base import utcnow
from app.db.models import AgentIdentity
from app.kernel import budget, revocation, velocity
from app.kernel.reasons import Decision, ReasonCode
from app.kernel.verdict import KernelAction, Verdict, allow, refuse
from app.trust.registry import UNVERIFIED_TIER, Tier

RESTRICTED_CATEGORIES: frozenset[str] = frozenset({"alcohol", "restricted-blades"})


@dataclass(frozen=True)
class CartItem:
    sku: str
    quantity: int
    category: str
    unit_price_minor: int
    min_order_quantity: int = 1
    max_order_quantity: int = 10
    age_restricted: bool = False
    region_lock: tuple[str, ...] = ()


@dataclass
class KernelInput:
    action: str
    agent_id: str
    amount_minor: int
    currency: str = "INR"
    tier: Tier | None = None
    agent: AgentIdentity | None = None
    checkout_id: str | None = None
    mandate_id: str | None = None
    items: list[CartItem] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    policy_hash_live: str | None = None
    policy_hash_acknowledged: str | None = None
    buyer_region: str | None = None
    verified: bool = False
    correlation_id: str = ""
    now: datetime = field(default_factory=utcnow)
    reserve_budget: bool = True

    @property
    def categories(self) -> set[str]:
        return {item.category for item in self.items}


@dataclass
class KernelResult:
    verdict: Verdict
    reservation_id: str | None = None
    unresolved_constraints: list[ConstraintResult] = field(default_factory=list)
    challenge: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict.allowed

    @property
    def escalates(self) -> bool:
        return self.verdict.decision is Decision.ESCALATE


def _challenge_for(tier_name: str, ceiling_minor: int, currency: str) -> dict[str, Any]:
    """A machine-readable statement of exactly which credentials unlock the attempt."""
    return {
        "reason": "credentials_required",
        "current_tier": tier_name,
        "ceiling": {"amount": ceiling_minor, "currency": currency},
        "accepted_credentials": [
            {
                "vct": "mandate.checkout.open.1",
                "format": "dc+sd-jwt",
                "signed_by": "human",
                "presented_with": "mandate.checkout.1",
            },
            {
                "vct": "mandate.payment.open.1",
                "format": "dc+sd-jwt",
                "signed_by": "human",
                "presented_with": "mandate.payment.1",
            },
        ],
        "required_issuer_tiers": ["provisional", "accredited", "regulated"],
        "key_binding": "required",
        "presentation_header": "X-AP2-Credentials",
    }


def evaluate(session: Session, request: KernelInput) -> KernelResult:
    """Run the fixed order above. Returns at the first refusal."""
    base: dict[str, Any] = {
        "action": request.action,
        "agent_id": request.agent_id,
        "amount_minor": request.amount_minor,
        "currency": request.currency,
        "checkout_id": request.checkout_id,
        "mandate_id": request.mandate_id,
    }
    tier = request.tier
    tier_name = tier.name if tier else UNVERIFIED_TIER

    def deny(code: ReasonCode, **evidence: Any) -> KernelResult:
        return KernelResult(
            verdict=refuse(
                code,
                request.action,
                request.agent_id,
                amount_minor=request.amount_minor,
                currency=request.currency,
                checkout_id=request.checkout_id,
                mandate_id=request.mandate_id,
                correlation_id=request.correlation_id,
                evidence={**base, "tier": tier_name, **evidence},
            )
        )

    # 1. kill switch
    if request.agent is not None and request.agent.kill_switch:
        return deny(ReasonCode.AGENT_KILL_SWITCH, step="kill_switch")

    # 2. revocation, read as late as possible
    revocation_state = revocation.check(session, request.mandate_id)
    if revocation_state.revoked:
        return deny(
            ReasonCode.MANDATE_REVOKED, step="revocation", revocation=revocation_state.as_evidence()
        )

    # 3. policy acknowledgment
    if (
        request.policy_hash_live is not None
        and request.policy_hash_acknowledged != request.policy_hash_live
    ):
        return deny(
            ReasonCode.POLICY_HASH_MISMATCH,
            step="policy_acknowledgment",
            acknowledged=request.policy_hash_acknowledged,
            live=request.policy_hash_live,
        )

    # 4. per-item policy
    for item in request.items:
        if item.quantity < item.min_order_quantity or item.quantity > item.max_order_quantity:
            return deny(
                ReasonCode.QUANTITY_OUT_OF_RANGE,
                step="item_policy",
                sku=item.sku,
                quantity=item.quantity,
                min_order_quantity=item.min_order_quantity,
                max_order_quantity=item.max_order_quantity,
            )
        if item.age_restricted and not (tier and tier.allow_age_restricted):
            code = (
                ReasonCode.UNVERIFIED_CATEGORY_FORBIDDEN
                if tier_name == UNVERIFIED_TIER
                else ReasonCode.ITEM_AGE_RESTRICTED
            )
            result = deny(code, step="item_policy", sku=item.sku, tier=tier_name)
            if code is ReasonCode.UNVERIFIED_CATEGORY_FORBIDDEN:
                result.challenge = _challenge_for(tier_name, 0, request.currency)
            return result
        if item.category in RESTRICTED_CATEGORIES and not (
            tier and tier.allow_restricted_categories
        ):
            code = (
                ReasonCode.UNVERIFIED_CATEGORY_FORBIDDEN
                if tier_name == UNVERIFIED_TIER
                else ReasonCode.CATEGORY_FORBIDDEN
            )
            result = deny(code, step="item_policy", sku=item.sku, category=item.category)
            if code is ReasonCode.UNVERIFIED_CATEGORY_FORBIDDEN:
                result.challenge = _challenge_for(tier_name, 0, request.currency)
            return result
        if request.buyer_region and request.buyer_region in item.region_lock:
            return deny(
                ReasonCode.ITEM_REGION_LOCKED,
                step="item_policy",
                sku=item.sku,
                region=request.buyer_region,
            )

    # 5. tier ceiling. An unverified agent gets a smaller door, not a closed one.
    if tier is not None and request.amount_minor > tier.max_transaction_minor:
        if tier_name == UNVERIFIED_TIER:
            result = KernelResult(
                verdict=refuse(
                    ReasonCode.UNVERIFIED_CEILING_EXCEEDED,
                    request.action,
                    request.agent_id,
                    decision=Decision.CHALLENGE,
                    amount_minor=request.amount_minor,
                    currency=request.currency,
                    checkout_id=request.checkout_id,
                    mandate_id=request.mandate_id,
                    correlation_id=request.correlation_id,
                    evidence={
                        **base,
                        "tier": tier_name,
                        "step": "tier_ceiling",
                        "ceiling_minor": tier.max_transaction_minor,
                    },
                )
            )
            result.challenge = _challenge_for(
                tier_name, tier.max_transaction_minor, request.currency
            )
            return result
        return deny(
            ReasonCode.CRED_ISSUER_TIER_INSUFFICIENT,
            step="tier_ceiling",
            ceiling_minor=tier.max_transaction_minor,
        )

    # 6. deterministic constraint satisfaction
    violation = first_violation(request.constraint_results)
    if violation is not None:
        return deny(
            violation.reason_code or ReasonCode.CONSTRAINT_UNRESOLVED,
            step="constraints",
            constraint=violation.as_evidence(),
        )

    # 7. merchant-set agent controls
    gate = velocity.check_agent_controls(
        session,
        request.agent,
        amount_minor=request.amount_minor,
        categories=request.categories,
        now=request.now,
    )
    if not gate.allowed:
        mapping = {
            "kill_switch": ReasonCode.AGENT_KILL_SWITCH,
            "category_blocked": ReasonCode.CATEGORY_FORBIDDEN,
            "category_not_allowed": ReasonCode.CATEGORY_FORBIDDEN,
            "spend_window": ReasonCode.VELOCITY_SPEND_EXCEEDED,
            "count_window": ReasonCode.VELOCITY_COUNT_EXCEEDED,
        }
        return deny(
            mapping.get(gate.reason or "", ReasonCode.VELOCITY_SPEND_EXCEEDED),
            step="agent_controls",
            gate=gate.reason,
            detail=gate.detail,
        )

    # 8. structuring across the rolling window
    if request.amount_minor > 0 and tier is not None:
        finding = velocity.detect_structuring(
            session,
            agent_id=request.agent_id,
            mandate_id=request.mandate_id,
            pending_amount_minor=request.amount_minor,
            per_transaction_cap_minor=tier.max_transaction_minor,
            now=request.now,
        )
        if finding.detected:
            return deny(
                ReasonCode.STRUCTURING_DETECTED,
                step="structuring",
                window_spend_minor=finding.window_spend_minor,
                per_transaction_cap_minor=finding.per_transaction_cap_minor,
                transaction_count=finding.transaction_count,
            )

    # 9. budget reservation under a row lock
    reservation_id: str | None = None
    if request.reserve_budget and request.mandate_id and request.amount_minor > 0:
        try:
            reservation = budget.reserve(
                session,
                request.mandate_id,
                request.amount_minor,
                request.correlation_id or request.checkout_id or request.agent_id,
            )
            reservation_id = reservation.id
        except budget.BudgetExceeded as exc:
            return deny(
                ReasonCode.BUDGET_EXCEEDED,
                step="budget",
                cap_minor=exc.state.cap_minor,
                committed_minor=exc.state.committed_minor,
                reserved_minor=exc.state.reserved_minor,
                requested_minor=exc.requested_minor,
            )
        except LookupError:
            return deny(ReasonCode.MANDATE_UNKNOWN, step="budget")

    # 10. anything undecided escalates. It never becomes an approval.
    pending = unresolved(request.constraint_results)
    if pending:
        return KernelResult(
            verdict=refuse(
                ReasonCode.CONSTRAINT_UNRESOLVED,
                request.action,
                request.agent_id,
                decision=Decision.ESCALATE,
                amount_minor=request.amount_minor,
                currency=request.currency,
                checkout_id=request.checkout_id,
                mandate_id=request.mandate_id,
                correlation_id=request.correlation_id,
                evidence={
                    **base,
                    "tier": tier_name,
                    "step": "unresolved_constraints",
                    "constraints": [c.as_evidence() for c in pending],
                },
            ),
            reservation_id=reservation_id,
            unresolved_constraints=pending,
        )

    approval = (
        ReasonCode.APPROVED
        if request.verified
        else ReasonCode.APPROVED_WITHIN_UNVERIFIED_CEILING
    )
    return KernelResult(
        verdict=allow(
            approval,
            request.action,
            request.agent_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            checkout_id=request.checkout_id,
            mandate_id=request.mandate_id,
            correlation_id=request.correlation_id,
            evidence={
                **base,
                "tier": tier_name,
                "constraints": [c.as_evidence() for c in request.constraint_results],
            },
        ),
        reservation_id=reservation_id,
    )


__all__ = [
    "CartItem",
    "KernelAction",
    "KernelInput",
    "KernelResult",
    "evaluate",
]
