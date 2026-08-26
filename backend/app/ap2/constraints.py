"""Deterministic evaluation of AP2 open-mandate constraints.

This is step 7 of the verification pipeline: does the closed mandate satisfy every constraint the
human expressed in the open mandate. Only constraints that are numeric, temporal, categorical or
set-membership based are decided here. Anything this module does not recognise returns
``UNRESOLVED`` and is routed to the semantic path. Nothing here ever returns satisfied for a
constraint it did not actually evaluate.

No model client is reachable from this module, and the kernel isolation test enforces that.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from app.ap2.models import Checkout, Merchant
from app.ap2.vocabulary import (
    NATURAL_LANGUAGE_CONSTRAINT,
    CheckoutConstraint,
    PaymentConstraint,
)
from app.kernel.reasons import ReasonCode


class ConstraintOutcome(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ConstraintResult:
    constraint_type: str
    outcome: ConstraintOutcome
    reason_code: ReasonCode | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return self.outcome is ConstraintOutcome.SATISFIED

    def as_evidence(self) -> dict[str, Any]:
        return {
            "constraint_type": self.constraint_type,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "detail": self.detail,
        }


@dataclass
class MandateUsage:
    """Aggregated prior use of one open mandate, for recurrence and budget constraints."""

    total_amount_minor: int = 0
    total_uses: int = 0


def _satisfied(kind: str, **detail: Any) -> ConstraintResult:
    return ConstraintResult(kind, ConstraintOutcome.SATISFIED, None, detail)


def _violated(kind: str, code: ReasonCode, **detail: Any) -> ConstraintResult:
    return ConstraintResult(kind, ConstraintOutcome.VIOLATED, code, detail)


def _unresolved(kind: str, **detail: Any) -> ConstraintResult:
    return ConstraintResult(
        kind, ConstraintOutcome.UNRESOLVED, ReasonCode.CONSTRAINT_UNRESOLVED, detail
    )


def merchant_matches(candidate: dict[str, Any] | Merchant, target: Merchant) -> bool:
    """Match on id when both carry one, otherwise on name plus website.

    Mirrors the reference implementation's rule so an allowlist written for the reference agent
    behaves identically here.
    """
    if isinstance(candidate, Merchant):
        cand_id, cand_name, cand_site = candidate.id, candidate.name, candidate.website
    else:
        cand_id = candidate.get("id")
        cand_name = candidate.get("name")
        cand_site = candidate.get("website")
    if cand_id and target.id:
        return cand_id == target.id
    return (
        bool(cand_name)
        and cand_name == target.name
        and bool(cand_site)
        and cand_site == target.website
    )


# --- bipartite feasibility for line-item requirements ------------------------------------------


def _max_flow(
    requirements: Sequence[tuple[str, int, set[str]]],
    supply: Sequence[tuple[str, str, int]],
) -> tuple[int, dict[str, int]]:
    """Maximum assignment of cart units to requirements.

    A greedy match produces false violations when one cart line could satisfy several
    requirements, so this solves the assignment properly. Graph: source to each requirement with
    capacity equal to the required quantity, requirement to each eligible cart line with unbounded
    capacity, each cart line to sink with capacity equal to its quantity.
    """
    nodes: dict[str, int] = {"__source__": 0, "__sink__": 1}

    def node(name: str) -> int:
        if name not in nodes:
            nodes[name] = len(nodes)
        return nodes[name]

    edges: list[list[int]] = []  # [to, capacity, reverse_index]
    graph: dict[int, list[int]] = {}

    def add(u: int, v: int, capacity: int) -> None:
        graph.setdefault(u, []).append(len(edges))
        edges.append([v, capacity, len(edges) + 1])
        graph.setdefault(v, []).append(len(edges))
        edges.append([u, 0, len(edges) - 1])

    source, sink = 0, 1
    total_required = 0
    for req_id, quantity, _ in requirements:
        add(source, node(f"r:{req_id}"), quantity)
        total_required += quantity
    for line_id, _item_id, quantity in supply:
        add(node(f"l:{line_id}"), sink, quantity)
    for req_id, _quantity, acceptable in requirements:
        for line_id, item_id, _q in supply:
            if item_id in acceptable:
                add(node(f"r:{req_id}"), node(f"l:{line_id}"), total_required or 1)

    flow = 0
    while True:
        parent: dict[int, int] = {source: -1}
        queue = [source]
        while queue and sink not in parent:
            current = queue.pop(0)
            for edge_index in graph.get(current, []):
                to, capacity, _ = edges[edge_index]
                if capacity > 0 and to not in parent:
                    parent[to] = edge_index
                    queue.append(to)
        if sink not in parent:
            break
        path: list[int] = []
        cursor = sink
        while cursor != source:
            edge_index = parent[cursor]
            path.append(edge_index)
            cursor = edges[edges[edge_index][2]][0]
        bottleneck = min(edges[i][1] for i in path)
        for i in path:
            edges[i][1] -= bottleneck
            edges[edges[i][2]][1] += bottleneck
        flow += bottleneck

    consumed: dict[str, int] = {}
    for line_id, _item_id, quantity in supply:
        index = next(i for i in graph[nodes[f"l:{line_id}"]] if edges[i][0] == sink)
        consumed[line_id] = quantity - edges[index][1]
    return flow, consumed


def _evaluate_line_items(constraint: dict[str, Any], checkout: Checkout) -> ConstraintResult:
    kind = CheckoutConstraint.LINE_ITEMS.value
    raw_items = constraint.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return _violated(kind, ReasonCode.CONSTRAINT_LINE_ITEM_UNSATISFIED, error="no requirements")

    requirements: list[tuple[str, int, set[str]]] = []
    for requirement in raw_items:
        if not isinstance(requirement, dict):
            return _violated(kind, ReasonCode.CONSTRAINT_LINE_ITEM_UNSATISFIED, error="malformed")
        acceptable = {
            entry.get("id")
            for entry in requirement.get("acceptable_items", []) or []
            if isinstance(entry, dict) and entry.get("id")
        }
        requirements.append(
            (str(requirement.get("id")), int(requirement.get("quantity", 0)), acceptable)
        )

    supply = [(line.id, line.item.id, line.quantity) for line in checkout.line_items]
    required_total = sum(quantity for _, quantity, _ in requirements)
    flow, consumed = _max_flow(requirements, supply)

    if flow < required_total:
        unmet = [
            {"requirement_id": rid, "quantity": qty, "acceptable_items": sorted(acc)}
            for rid, qty, acc in requirements
        ]
        return _violated(
            kind,
            ReasonCode.CONSTRAINT_LINE_ITEM_UNSATISFIED,
            matched_units=flow,
            required_units=required_total,
            requirements=unmet,
        )

    # An item the human never authorised must not ride along inside an authorised cart. The
    # published constraint only says which items must be present; refusing unlisted extras is a
    # deliberate tightening of it.
    unauthorised = [
        {
            "line_id": line.id,
            "item_id": line.item.id,
            "quantity": line.quantity - consumed.get(line.id, 0),
        }
        for line in checkout.line_items
        if line.quantity - consumed.get(line.id, 0) > 0
    ]
    if unauthorised:
        return _violated(
            kind,
            ReasonCode.CONSTRAINT_LINE_ITEM_UNSATISFIED,
            unauthorised_units=unauthorised,
            error="cart contains units no line-item requirement authorises",
        )
    return _satisfied(kind, matched_units=flow)


# --- checkout constraints ----------------------------------------------------------------------


def evaluate_checkout_constraints(
    constraints: Iterable[dict[str, Any]],
    checkout: Checkout,
    merchant: Merchant,
) -> list[ConstraintResult]:
    results: list[ConstraintResult] = []
    for constraint in constraints:
        if not isinstance(constraint, dict):
            results.append(_unresolved("<malformed>", value=str(constraint)[:200]))
            continue
        kind = str(constraint.get("type", "<missing>"))

        if kind == CheckoutConstraint.ALLOWED_MERCHANTS:
            allowed = constraint.get("allowed") or []
            if any(merchant_matches(entry, merchant) for entry in allowed):
                results.append(_satisfied(kind, merchant_id=merchant.id))
            else:
                results.append(
                    _violated(
                        kind,
                        ReasonCode.CONSTRAINT_MERCHANT_NOT_ALLOWED,
                        merchant_id=merchant.id,
                        allowed_ids=[e.get("id") for e in allowed if isinstance(e, dict)],
                    )
                )
        elif kind == CheckoutConstraint.LINE_ITEMS:
            results.append(_evaluate_line_items(constraint, checkout))
        elif kind == NATURAL_LANGUAGE_CONSTRAINT:
            results.append(
                _unresolved(kind, text=str(constraint.get("text", ""))[:500], natural_language=True)
            )
        else:
            results.append(_unresolved(kind, reason="unknown constraint type"))
    return results


# --- payment constraints -----------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def evaluate_payment_constraints(
    constraints: Iterable[dict[str, Any]],
    *,
    amount_minor: int,
    currency: str,
    payee: Merchant,
    instrument_id: str | None,
    pisp_id: str | None,
    open_checkout_digest: str | None,
    usage: MandateUsage,
    now: datetime,
) -> list[ConstraintResult]:
    results: list[ConstraintResult] = []
    for constraint in constraints:
        if not isinstance(constraint, dict):
            results.append(_unresolved("<malformed>", value=str(constraint)[:200]))
            continue
        kind = str(constraint.get("type", "<missing>"))

        if kind == PaymentConstraint.AMOUNT_RANGE:
            expected = constraint.get("currency")
            if expected and expected != currency:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_CURRENCY_MISMATCH,
                              expected=expected, actual=currency)
                )
                continue
            maximum = constraint.get("max")
            minimum = constraint.get("min")
            if maximum is not None and amount_minor > int(maximum):
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_AMOUNT_EXCEEDED,
                              amount_minor=amount_minor, max_minor=int(maximum))
                )
            elif minimum is not None and amount_minor < int(minimum):
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_AMOUNT_BELOW_MINIMUM,
                              amount_minor=amount_minor, min_minor=int(minimum))
                )
            else:
                results.append(_satisfied(kind, amount_minor=amount_minor))

        elif kind == PaymentConstraint.BUDGET:
            expected = constraint.get("currency")
            if expected and expected != currency:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_CURRENCY_MISMATCH,
                              expected=expected, actual=currency)
                )
                continue
            # The published schema types budget.max as a number, so it is read in minor units and
            # rounded down rather than assumed to be an integer already.
            cap = int(float(constraint.get("max", 0)))
            projected = usage.total_amount_minor + amount_minor
            if projected > cap:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_BUDGET_EXCEEDED,
                              already_spent_minor=usage.total_amount_minor,
                              amount_minor=amount_minor, budget_minor=cap)
                )
            else:
                results.append(_satisfied(kind, remaining_minor=cap - projected))

        elif kind == PaymentConstraint.ALLOWED_PAYEES:
            allowed = constraint.get("allowed") or []
            if any(merchant_matches(entry, payee) for entry in allowed):
                results.append(_satisfied(kind, payee_id=payee.id))
            else:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_PAYEE_NOT_ALLOWED, payee_id=payee.id)
                )

        elif kind == PaymentConstraint.ALLOWED_PAYMENT_INSTRUMENTS:
            allowed_ids = {
                e.get("id") for e in (constraint.get("allowed") or []) if isinstance(e, dict)
            }
            if instrument_id and instrument_id in allowed_ids:
                results.append(_satisfied(kind, instrument_id=instrument_id))
            else:
                results.append(
                    _violated(
                        kind,
                        ReasonCode.CONSTRAINT_INSTRUMENT_NOT_ALLOWED,
                        instrument_id=instrument_id,
                        allowed=sorted(i for i in allowed_ids if i),
                    )
                )

        elif kind == PaymentConstraint.ALLOWED_PISPS:
            allowed_ids = {
                e.get("id") for e in (constraint.get("allowed") or []) if isinstance(e, dict)
            }
            if pisp_id is None or pisp_id in allowed_ids:
                results.append(_satisfied(kind, pisp_id=pisp_id))
            else:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_PISP_NOT_ALLOWED, pisp_id=pisp_id)
                )

        elif kind == PaymentConstraint.REFERENCE:
            expected_digest = constraint.get("conditional_transaction_id")
            if open_checkout_digest and expected_digest == open_checkout_digest:
                results.append(_satisfied(kind))
            else:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_PAYMENT_REFERENCE_MISMATCH,
                              expected=expected_digest, actual=open_checkout_digest)
                )

        elif kind == PaymentConstraint.AGENT_RECURRENCE:
            limit = constraint.get("max_occurrences")
            if limit is not None and usage.total_uses >= int(limit):
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_RECURRENCE_EXHAUSTED,
                              uses=usage.total_uses, max_occurrences=int(limit))
                )
            else:
                results.append(_satisfied(kind, uses=usage.total_uses))

        elif kind == PaymentConstraint.EXECUTION_DATE:
            not_before = constraint.get("not_before")
            not_after = constraint.get("not_after")
            parsed_before = _parse_iso(not_before) if not_before else None
            parsed_after = _parse_iso(not_after) if not_after else None
            if (not_before and parsed_before is None) or (not_after and parsed_after is None):
                results.append(_unresolved(kind, reason="unparseable execution window"))
            elif parsed_before and now < parsed_before:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_EXECUTION_WINDOW,
                              not_before=not_before, now=now.isoformat())
                )
            elif parsed_after and now > parsed_after:
                results.append(
                    _violated(kind, ReasonCode.CONSTRAINT_EXECUTION_WINDOW,
                              not_after=not_after, now=now.isoformat())
                )
            else:
                results.append(_satisfied(kind))

        elif kind == NATURAL_LANGUAGE_CONSTRAINT:
            results.append(
                _unresolved(kind, text=str(constraint.get("text", ""))[:500], natural_language=True)
            )
        else:
            results.append(_unresolved(kind, reason="unknown constraint type"))
    return results


def first_violation(results: Sequence[ConstraintResult]) -> ConstraintResult | None:
    return next((r for r in results if r.outcome is ConstraintOutcome.VIOLATED), None)


def unresolved(results: Sequence[ConstraintResult]) -> list[ConstraintResult]:
    return [r for r in results if r.outcome is ConstraintOutcome.UNRESOLVED]
