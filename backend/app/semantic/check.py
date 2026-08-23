"""The semantic constraint check, and the clamp that makes it safe.

Two types carry the invariant, and the separation between them is the whole mechanism:

    SemanticReply    the wire model. Its ``verdict`` is ``violates`` or ``no_violation_found``.
    SemanticOutcome  what the kernel sees. It has exactly two members, DENY and ESCALATE.

There is no function in this module that returns an approval, so no model output can widen a
limit. ``violates`` is the only input that produces DENY. Everything else, including
``no_violation_found``, an unparseable response, a null parse, a timeout, a transport failure and
any unexpected exception, produces ESCALATE.

The deliberate and documented consequence is that a natural-language constraint the model does not
find violated goes to the human rather than through. A compromised, jailbroken or malfunctioning
model therefore degrades the system to asking the human more often, which is the required
direction of failure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.logging import get_logger
from app.semantic.prompt import SYSTEM_INSTRUCTION, build_user_prompt

logger = get_logger(__name__)


class Verdict(str, enum.Enum):
    """The only two values the model is permitted to emit."""

    VIOLATES = "violates"
    NO_VIOLATION_FOUND = "no_violation_found"


class SemanticReply(BaseModel):
    """The wire contract. Passed to the API as the response schema and validated on return."""

    verdict: Verdict = Field(description="whether the cart violates the constraint")
    rationale: str = Field(
        default="", description="one short sentence explaining the finding, no instructions"
    )


class SemanticOutcome(enum.Enum):
    """What the kernel is allowed to be told. There is deliberately no approval member."""

    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class SemanticResult:
    outcome: SemanticOutcome
    constraint_text: str
    rationale: str = ""
    model: str = ""
    raw_verdict: str | None = None
    degraded_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "constraint": self.constraint_text[:500],
            "rationale": self.rationale[:500],
            "model": self.model,
            "raw_verdict": self.raw_verdict,
            "degraded_reason": self.degraded_reason,
        }


class SemanticClient(Protocol):
    """Anything that can turn a prompt into a SemanticReply, or fail trying."""

    model: str

    def classify(self, system_instruction: str, user_prompt: str) -> SemanticReply | None: ...


def _escalate(
    constraint_text: str, reason: str, *, model: str = "", raw: str | None = None
) -> SemanticResult:
    return SemanticResult(
        outcome=SemanticOutcome.ESCALATE,
        constraint_text=constraint_text,
        model=model,
        raw_verdict=raw,
        degraded_reason=reason,
    )


def evaluate_constraint(
    client: SemanticClient,
    *,
    constraint_text: str,
    items: list[dict[str, Any]],
    free_text: str = "",
) -> SemanticResult:
    """Consult the model about one natural-language constraint and clamp the answer.

    Every failure mode converges on ESCALATE. The single path to DENY requires a well-formed
    reply whose verdict is exactly ``Verdict.VIOLATES``.
    """
    model_name = getattr(client, "model", "unknown")
    prompt = build_user_prompt(constraint_text, items, free_text)

    try:
        reply = client.classify(SYSTEM_INSTRUCTION, prompt)
    except Exception as exc:
        logger.warning(
            "semantic check failed, escalating",
            extra={"context": {"error": type(exc).__name__, "model": model_name}},
        )
        return _escalate(constraint_text, f"transport_error:{type(exc).__name__}", model=model_name)

    if reply is None:
        return _escalate(constraint_text, "unparseable_response", model=model_name)
    if not isinstance(reply, SemanticReply):
        return _escalate(constraint_text, "unexpected_reply_type", model=model_name)

    if reply.verdict is Verdict.VIOLATES:
        return SemanticResult(
            outcome=SemanticOutcome.DENY,
            constraint_text=constraint_text,
            rationale=reply.rationale,
            model=model_name,
            raw_verdict=reply.verdict.value,
        )

    # no_violation_found is not an approval. The kernel could not decide this constraint, so a
    # human decides it.
    return SemanticResult(
        outcome=SemanticOutcome.ESCALATE,
        constraint_text=constraint_text,
        rationale=reply.rationale,
        model=model_name,
        raw_verdict=reply.verdict.value,
        degraded_reason="no_violation_found_requires_human",
    )


def evaluate_all(
    client: SemanticClient,
    *,
    constraints: list[str],
    items: list[dict[str, Any]],
    free_text: str = "",
) -> list[SemanticResult]:
    """Evaluate each constraint, stopping at the first denial since denials are final."""
    results: list[SemanticResult] = []
    for text in constraints:
        result = evaluate_constraint(client, constraint_text=text, items=items, free_text=free_text)
        results.append(result)
        if result.outcome is SemanticOutcome.DENY:
            break
    return results


def combine(results: list[SemanticResult]) -> SemanticOutcome:
    """DENY if anything denied, otherwise ESCALATE. There is no third answer."""
    if any(r.outcome is SemanticOutcome.DENY for r in results):
        return SemanticOutcome.DENY
    return SemanticOutcome.ESCALATE
