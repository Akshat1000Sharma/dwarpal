"""Turn a sentence into a cart and the authority to buy it.

Two planners, one interface. Gemini reads the live catalog and picks; a deterministic planner does
the same job by search when there is no model, which is what tests and CI use so neither ever
needs a key.

Whatever the planner returns is treated as a suggestion, not an instruction. `validate` re-reads
every SKU from the catalog, drops anything that does not exist, clamps quantities to the item's
own declared range, and recomputes the total from merchant prices. A model that hallucinated a
SKU or a price cannot put either on the wire.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.catalog import service as catalog
from app.logging import get_logger
from app.settings import settings

logger = get_logger(__name__)

# What the human's standing authority allows when they did not say. Generous enough that an
# ordinary basket fits, small enough that a runaway agent is stopped by the mandate itself.
DEFAULT_BUDGET_MINOR = 2_000_000
MAX_LINES = 6


class PlannedLine(BaseModel):
    sku: str = Field(max_length=64)
    quantity: int = Field(ge=1, le=99)


class PlannedCart(BaseModel):
    """The shape Gemini is constrained to produce."""

    lines: list[PlannedLine] = Field(default_factory=list)
    budget_cap_minor: int = Field(default=DEFAULT_BUDGET_MINOR, ge=0)
    natural_language: list[str] = Field(default_factory=list)
    rationale: str = ""


@dataclass
class BuyerPlan:
    """A validated cart, safe to quote."""

    lines: list[tuple[str, str, int]] = field(default_factory=list)
    budget_cap_minor: int = DEFAULT_BUDGET_MINOR
    natural_language: list[str] = field(default_factory=list)
    rationale: str = ""
    planner: str = "rule-based"
    dropped: list[dict[str, Any]] = field(default_factory=list)
    estimated_total_minor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "lines": [
                {"sku": sku, "title": title, "quantity": qty} for sku, title, qty in self.lines
            ],
            "budget_cap_minor": self.budget_cap_minor,
            "natural_language": self.natural_language,
            "rationale": self.rationale,
            "dropped": self.dropped,
            "estimated_total_minor": self.estimated_total_minor,
        }


class BuyerPlanner(Protocol):
    name: str

    def propose(self, prompt: str, catalog_document: list[dict[str, Any]]) -> PlannedCart: ...


def catalog_for_planning(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    """The catalog as the buyer's agent sees it: enough to choose, nothing it must not act on."""
    entries = catalog.browse(session, limit=limit)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        product = entry.product
        rows.append(
            {
                "sku": product.sku,
                "title": product.title,
                "description": product.description,
                "category": product.category,
                "price_minor": product.price_minor,
                "currency": product.currency,
                "max_order_quantity": product.max_order_quantity,
                "age_restricted": product.age_restricted,
                "perishable": product.perishable,
            }
        )
    return rows


SYSTEM_INSTRUCTION = (
    "You are a shopping agent buying on behalf of a person. You are given a merchant catalog and "
    "the person's request. Choose only SKUs that appear verbatim in the catalog. Respect any "
    "budget the person names, in rupees, converted to paise (multiply by 100). If the person "
    "states a preference that cannot be checked arithmetically, such as avoiding perishable "
    "goods, copy it into natural_language as a short instruction rather than trying to enforce it "
    "yourself. Never invent a SKU. If nothing in the catalog fits, return no lines."
)


class GeminiBuyerPlanner:
    """Constrained decoding against the live catalog. One call, zero temperature."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from google import genai

        self.model = model or settings.GEMINI_MODEL
        self._client = genai.Client(api_key=api_key or settings.GEMINI_API_KEY)

    def propose(self, prompt: str, catalog_document: list[dict[str, Any]]) -> PlannedCart:
        from google.genai import types

        user_prompt = (
            "<catalog>\n"
            + json.dumps(catalog_document, indent=2)
            + "\n</catalog>\n\n<request>\n"
            + prompt.strip()[:2000]
            + "\n</request>"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=PlannedCart,
                http_options=types.HttpOptions(
                    timeout=int(settings.SEMANTIC_TIMEOUT_SECONDS * 1000)
                ),
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, PlannedCart):
            return parsed
        logger.warning(
            "buyer plan did not parse into the response schema",
            extra={"context": {"model": self.model}},
        )
        return PlannedCart(rationale="the model returned nothing usable")


# Phrases a person uses for a constraint arithmetic cannot settle. The rule-based planner copies
# them through so the merchant's semantic path is exercised without a model on either side.
_NL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("perishable", "nothing perishable"),
    ("fresh", "nothing perishable"),
    ("alcohol", "nothing alcoholic"),
    ("wine", "nothing alcoholic"),
    ("vegetarian", "vegetarian items only"),
    ("gift", "nothing that would be unsuitable as a gift"),
    ("age restricted", "nothing age restricted"),
    ("knife", "nothing bladed"),
)

_AMOUNT = re.compile(
    r"(?:under|below|less than|up to|max(?:imum)?(?: of)?)\s*(?:rs\.?|inr)?\s*([\d,]+)",
    re.I,
)


class RuleBasedPlanner:
    """Deterministic catalog search. No network, identical output every run.

    Used by tests, by CI and whenever the model is unavailable. It is deliberately simple: the
    thing under test is the merchant's gate, not the shopper's taste.
    """

    name = "rule-based"

    def propose(self, prompt: str, catalog_document: list[dict[str, Any]]) -> PlannedCart:
        lowered = prompt.lower()

        budget = DEFAULT_BUDGET_MINOR
        match = _AMOUNT.search(lowered)
        if match:
            budget = int(match.group(1).replace(",", "")) * 100

        natural = []
        for needle, phrase in _NL_PATTERNS:
            if needle in lowered and phrase not in natural:
                natural.append(phrase)

        # Score every item by how many of the request's words it matches, then take the best few
        # that fit inside the budget.
        words = set(re.findall(r"[a-z]{3,}", lowered)) - _STOPWORDS
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in catalog_document:
            haystack = f"{item['title']} {item['description']} {item['category']}".lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["price_minor"]))

        lines: list[PlannedLine] = []
        running = 0
        wanted = 1
        quantity_match = re.search(r"\b(\d+)\s+(?:packets?|units?|of|x)\b", lowered)
        if quantity_match:
            wanted = max(1, min(9, int(quantity_match.group(1))))

        for _score, item in scored:
            if len(lines) >= MAX_LINES:
                break
            quantity = min(wanted, item["max_order_quantity"])
            cost = item["price_minor"] * quantity
            if running + cost > budget:
                continue
            lines.append(PlannedLine(sku=item["sku"], quantity=quantity))
            running += cost

        if not lines and catalog_document:
            # Nothing matched the words, so fall back to the cheapest thing that fits. An empty
            # cart teaches the operator nothing; a small one exercises the whole path.
            cheapest = min(catalog_document, key=lambda i: i["price_minor"])
            if cheapest["price_minor"] <= budget:
                lines.append(PlannedLine(sku=cheapest["sku"], quantity=1))

        return PlannedCart(
            lines=lines,
            budget_cap_minor=budget,
            natural_language=natural,
            rationale=(
                f"matched {len(lines)} catalog item(s) against the request, "
                f"inside a budget of {budget / 100:,.2f}"
            ),
        )


_STOPWORDS = {
    "buy", "get", "please", "want", "need", "some", "for", "the", "and", "with", "under",
    "below", "less", "than", "max", "maximum", "rupees", "rupee", "inr", "order", "purchase",
    "something", "anything", "that", "this", "from", "shop", "store", "cart", "items", "item",
    "nothing", "avoid", "only", "just", "would", "like", "could", "should", "make", "sure",
}


def get_planner() -> BuyerPlanner:
    """The model when there is one, the deterministic planner otherwise.

    Tests and CI always land on the deterministic branch, so no test can require a Gemini key or
    make a network call.
    """
    if settings.APP_ENV == "testing":
        return RuleBasedPlanner()
    try:
        return GeminiBuyerPlanner()
    except Exception as exc:
        logger.warning(
            "falling back to the deterministic buyer planner",
            extra={"context": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        return RuleBasedPlanner()


def validate(
    session: Session,
    proposed: PlannedCart,
    *,
    planner_name: str,
    hard_cap_minor: int | None = None,
) -> BuyerPlan:
    """Re-read every line from the catalog. Model output never reaches the wire unchecked.

    ``hard_cap_minor`` is the buyer's own stated limit. Lines that would carry the cart past it are
    dropped, because the alternative is signing a standing authority larger than the human asked
    for, and a cap that silently grows to fit whatever the model chose is not a cap.
    """
    lines: list[tuple[str, str, int]] = []
    dropped: list[dict[str, Any]] = []
    total = 0

    for line in proposed.lines[:MAX_LINES]:
        entry = catalog.by_sku(session, line.sku)
        if entry is None:
            dropped.append({"sku": line.sku, "why": "no such item in the catalog"})
            continue
        product = entry.product
        quantity = max(product.min_order_quantity, min(line.quantity, product.max_order_quantity))
        if quantity != line.quantity:
            dropped.append(
                {
                    "sku": line.sku,
                    "why": (
                        f"quantity {line.quantity} clamped to {quantity} by the item's own "
                        f"declared range"
                    ),
                }
            )
        line_total = product.price_minor * quantity
        if hard_cap_minor is not None and total + line_total > hard_cap_minor:
            dropped.append(
                {
                    "sku": line.sku,
                    "why": (
                        f"dropped to stay inside the {hard_cap_minor / 100:,.2f} budget you set"
                    ),
                }
            )
            continue
        lines.append((product.sku, product.title, quantity))
        total += line_total

    if hard_cap_minor is not None:
        # The buyer named the limit, so it is the limit. The cart was trimmed to fit it above.
        budget = hard_cap_minor
    else:
        budget = proposed.budget_cap_minor or DEFAULT_BUDGET_MINOR
        # With no stated limit the mandate must still cover the cart it authorises, or the human
        # has signed something that can never be used.
        budget = max(budget, total)

    return BuyerPlan(
        lines=lines,
        budget_cap_minor=budget,
        natural_language=[
            str(t).strip()[:300] for t in proposed.natural_language if str(t).strip()
        ],
        rationale=str(proposed.rationale)[:1000],
        planner=planner_name,
        dropped=dropped,
        estimated_total_minor=total,
    )
