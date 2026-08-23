"""Gemini transport for the semantic check.

The official ``google-genai`` SDK is used rather than a hand-rolled HTTP call because Gemini
rejects a raw Pydantic ``model_json_schema()`` (it does not accept ``$defs``), so a direct call
would need a second, hand-maintained copy of the schema in the OpenAPI subset. Passing the Pydantic
class to ``response_schema`` keeps one source of truth and puts constrained decoding, not prompt
instructions, in charge of the output shape.

Constrained decoding is a useful defence, not the safety property. The safety property is in
``check.py``: the outcome type has no approval member.
"""

from __future__ import annotations

from functools import lru_cache

from app.logging import get_logger
from app.semantic.check import SemanticReply
from app.settings import settings

logger = get_logger(__name__)


class GeminiSemanticClient:
    """Calls Gemini once, with a zero temperature and an enum-constrained response schema."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from google import genai  # imported lazily so the kernel's import closure stays clean

        self.model = model or settings.GEMINI_MODEL
        self._genai = genai
        self._client = genai.Client(api_key=api_key or settings.GEMINI_API_KEY)

    def classify(self, system_instruction: str, user_prompt: str) -> SemanticReply | None:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=SemanticReply,
                http_options=types.HttpOptions(
                    timeout=int(settings.SEMANTIC_TIMEOUT_SECONDS * 1000)
                ),
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, SemanticReply):
            return parsed
        # A null or off-shape parse is not an error to recover from. Returning None sends the
        # caller down the escalation path, which is the required direction of failure.
        logger.warning(
            "semantic reply did not parse into the response schema",
            extra={"context": {"model": self.model, "parsed_type": type(parsed).__name__}},
        )
        return None


class StaticSemanticClient:
    """A deterministic client used by tests and the offline corpus runner.

    It never contacts a network. Tests must not require real Gemini credentials, and the corpus
    must produce the same scorecard on every run.
    """

    def __init__(
        self, reply: SemanticReply | None = None, *, error: Exception | None = None
    ) -> None:
        self.model = "static"
        self._reply = reply
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def classify(self, system_instruction: str, user_prompt: str) -> SemanticReply | None:
        self.calls.append((system_instruction, user_prompt))
        if self._error is not None:
            raise self._error
        return self._reply


class KeywordSemanticClient:
    """Offline stand-in that flags a violation on a keyword match.

    Used by the adversarial harness so the scorecard is reproducible without a model in the loop.
    It is deliberately crude: the corpus is testing the gate around the model, not the model.
    """

    def __init__(self, keywords: dict[str, tuple[str, ...]] | None = None) -> None:
        self.model = "keyword-offline"
        self.keywords = keywords or {
            "perishable": ("paneer", "mango", "fresh", "perishable", "chilled"),
            "alcohol": ("wine", "beer", "whisky", "alcohol"),
            "blade": ("knife", "blade"),
        }

    def classify(self, system_instruction: str, user_prompt: str) -> SemanticReply | None:
        lowered = user_prompt.lower()
        constraint = lowered.split("</constraint>")[0]
        cart = lowered.split("<untrusted_data>")[-1]
        for topic, words in self.keywords.items():
            if topic in constraint and any(word in cart for word in words):
                return SemanticReply(
                    verdict="violates",
                    rationale=f"cart contains an item matching the {topic} constraint",
                )
        return SemanticReply(verdict="no_violation_found", rationale="no matching item found")


@lru_cache(maxsize=1)
def get_client() -> GeminiSemanticClient:
    return GeminiSemanticClient()


def reset_cache() -> None:
    get_client.cache_clear()
