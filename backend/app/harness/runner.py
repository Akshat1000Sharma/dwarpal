"""Adversarial and benign corpus runner.

Scenarios are data, not hand-written test functions, so a new attack can be added by dropping a
YAML file in without touching any code. The runner turns each scenario into a real checkout
attempt against the real gate: the same verification pipeline, the same kernel, the same evidence
locker. Nothing here targets any external system; every request is fired at Dwarpal's own door.

The benign corpus runs alongside the adversarial one because a gate that blocks everything scores
perfectly against attacks and is useless. Both numbers are always reported together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.catalog import policy_terms
from app.checkout import quote
from app.checkout.complete import complete
from app.db.base import utcnow
from app.db.models import Product
from app.escalation.whatsapp import RecordingTransport
from app.harness import factory
from app.kernel import revocation
from app.kernel.reasons import ReasonCode
from app.payments.gateway import StubGateway
from app.semantic.client import KeywordSemanticClient
from app.settings import settings

CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"
ADVERSARIAL_DIR = CORPUS_ROOT / "adversarial"
BENIGN_DIR = CORPUS_ROOT / "benign"


@dataclass(frozen=True)
class Scenario:
    """One declarative case."""

    id: str
    family: str
    kind: str  # adversarial or benign
    description: str
    cart: list[dict[str, Any]]
    expect_blocked: bool
    expect_reason_codes: list[str] = field(default_factory=list)
    mandate: dict[str, Any] = field(default_factory=dict)
    tamper: dict[str, Any] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def cart_lines(self) -> list[tuple[str, str, int]]:
        return [(c["sku"], c.get("title", c["sku"]), int(c.get("quantity", 1))) for c in self.cart]


@dataclass
class ScenarioResult:
    scenario: Scenario
    blocked: bool
    reason_code: str
    status: str
    passed: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario.id,
            "family": self.scenario.family,
            "kind": self.scenario.kind,
            "description": self.scenario.description,
            "expected_blocked": self.scenario.expect_blocked,
            "expected_reason_codes": self.scenario.expect_reason_codes,
            "observed_blocked": self.blocked,
            "observed_reason_code": self.reason_code,
            "observed_status": self.status,
            "passed": self.passed,
            "note": self.note,
        }


def load_scenarios(directory: Path, kind: str) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        family = str(document.get("family", path.stem))
        for entry in document.get("scenarios", []) or []:
            scenarios.append(
                Scenario(
                    id=str(entry["id"]),
                    family=family,
                    kind=kind,
                    description=str(entry.get("description", "")),
                    cart=list(entry.get("cart", []) or []),
                    expect_blocked=bool(entry.get("expect_blocked", kind == "adversarial")),
                    expect_reason_codes=[
                        str(c) for c in entry.get("expect_reason_codes", []) or []
                    ],
                    mandate=dict(entry.get("mandate", {}) or {}),
                    tamper=dict(entry.get("tamper", {}) or {}),
                    setup=dict(entry.get("setup", {}) or {}),
                    source=path.name,
                )
            )
    return scenarios


def load_all() -> list[Scenario]:
    return [
        *load_scenarios(ADVERSARIAL_DIR, "adversarial"),
        *load_scenarios(BENIGN_DIR, "benign"),
    ]


def _spec_for(scenario: Scenario) -> factory.MandateSpec:
    mandate = scenario.mandate
    lines = scenario.cart_lines
    if mandate.get("authorises_cart") is False:
        lines = [("DWP-DOES-NOT-EXIST", "Something else entirely", 1)]
    spec = factory.spec_for_cart(
        lines,
        amount_cap_minor=int(mandate.get("amount_cap_minor", 5_000_000)),
        natural_language=list(mandate.get("natural_language", []) or []),
    )
    if mandate.get("budget_minor") is not None:
        spec.budget_minor = int(mandate["budget_minor"])
    if mandate.get("max_occurrences") is not None:
        spec.max_occurrences = int(mandate["max_occurrences"])
    if mandate.get("execution_not_before"):
        spec.execution_not_before = str(mandate["execution_not_before"])
    if mandate.get("execution_not_after"):
        spec.execution_not_after = str(mandate["execution_not_after"])
    return spec


def _tamper_for(scenario: Scenario) -> factory.Tamper:
    allowed = {f.name for f in factory.Tamper.__dataclass_fields__.values()}
    unknown = set(scenario.tamper) - allowed
    if unknown:
        raise ValueError(f"scenario {scenario.id} uses unknown tamper keys: {sorted(unknown)}")
    return factory.Tamper(**scenario.tamper)


def run_scenario(
    session: Session,
    scenario: Scenario,
    *,
    gateway: StubGateway | None = None,
) -> ScenarioResult:
    """Execute one scenario against the real gate and compare with what it declared."""
    gateway = gateway or StubGateway()
    correlation = f"corpus_{scenario.id}"
    issuer = scenario.mandate.get("issuer_id", factory.DEFAULT_ISSUER)
    principals = factory.Principals.create(
        issuer_id=issuer, agent_id=f"agent:{scenario.id}", register=True
    )
    spec = _spec_for(scenario)
    tamper = _tamper_for(scenario)

    setup = scenario.setup
    if setup.get("stock_override"):
        for sku, value in setup["stock_override"].items():
            product = session.query(Product).filter(Product.sku == sku).one_or_none()
            if product is not None:
                product.stock_total = int(value)
    session.flush()

    # The human signs the open mandates once; the agent presents them on each attempt. This
    # matters for the families that only appear across several attempts, such as recurrence
    # exhaustion and structuring.
    issued = factory.issue_open_mandates(principals, spec, tamper=tamper)
    repeat = int(setup.get("repeat_attempts", 1))
    last: ScenarioResult | None = None

    for attempt in range(repeat):
        try:
            quoted = quote.create_quote(
                session,
                agent_id=principals.agent_id,
                correlation_id=f"{correlation}_{attempt}",
                lines=[
                    quote.RequestedLine(sku=sku, quantity=qty)
                    for sku, _title, qty in scenario.cart_lines
                ],
            )
        except quote.QuoteError as exc:
            last = ScenarioResult(
                scenario=scenario,
                blocked=True,
                reason_code=exc.reason_code.value,
                status="refused_at_quote",
                passed=False,
                note=exc.message,
            )
            last.passed = _matches(scenario, last)
            continue

        if setup.get("policy_changes_after_quote"):
            original = policy_terms.read_terms_file
            policy_terms.read_terms_file = lambda: "# Revised terms\n\nThe terms have changed.\n"
            try:
                policy_terms.ensure_active_terms(session)
            finally:
                policy_terms.read_terms_file = original
            session.flush()

        presentation = factory.present_issued(
            issued,
            checkout_jwt=quoted.checkout_jwt,
            checkout_hash=quoted.checkout_hash,
            amount_minor=quoted.row.total_minor,
            tamper=tamper,
            nonce=f"nonce-{scenario.id}-{attempt}",
        )

        if setup.get("expire_quote"):
            quoted.row.expires_at = utcnow() - timedelta(seconds=1)
            session.flush()

        if setup.get("revoke_before_capture"):
            _revoke_mandate(session, presentation)

        outcome = complete(
            session,
            presentation.credentials,
            correlation_id=f"{correlation}_{attempt}",
            gateway=gateway,
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
        )

        if setup.get("replay_same_credentials"):
            outcome = complete(
                session,
                presentation.credentials,
                correlation_id=f"{correlation}_{attempt}_replay",
                gateway=gateway,
                semantic_client=KeywordSemanticClient(),
                whatsapp=RecordingTransport(),
            )

        blocked = outcome.status not in ("completed",)
        last = ScenarioResult(
            scenario=scenario,
            blocked=blocked,
            reason_code=outcome.reason_code.value,
            status=outcome.status,
            passed=False,
        )
        last.passed = _matches(scenario, last)
        session.flush()

    assert last is not None
    return last


def _revoke_mandate(session: Session, presentation: factory.Presentation) -> None:
    from app.db.models import OpenMandate
    from app.verification.pipeline import verify

    result = verify(session, presentation.credentials, record_nonce=False)
    if not result.ok or result.authority is None:
        return
    from app.checkout.complete import _upsert_open_mandate

    mandate = _upsert_open_mandate(session, result.authority)
    revocation.revoke(session, mandate.id, "revoked by the corpus")
    session.flush()
    del OpenMandate


def _matches(scenario: Scenario, result: ScenarioResult) -> bool:
    if scenario.expect_blocked != result.blocked:
        return False
    if scenario.expect_blocked and scenario.expect_reason_codes:
        return result.reason_code in scenario.expect_reason_codes
    return True


@dataclass
class CorpusReport:
    results: list[ScenarioResult]

    @property
    def adversarial(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.scenario.kind == "adversarial"]

    @property
    def benign(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.scenario.kind == "benign"]

    @property
    def blocked(self) -> list[ScenarioResult]:
        return [r for r in self.adversarial if r.blocked]

    @property
    def misses(self) -> list[ScenarioResult]:
        """Attacks that were not blocked, or were blocked for the wrong reason."""
        return [r for r in self.adversarial if not r.passed]

    @property
    def escalated(self) -> list[ScenarioResult]:
        """Legitimate traffic the merchant deliberately took to a human instead of completing.

        This is not a false positive. Asking the principal about a constraint the kernel cannot
        decide is the designed behaviour, and counting it as an error would make the merchant look
        wrong for refusing to guess.
        """
        return [r for r in self.benign if r.blocked and r.scenario.expect_blocked]

    @property
    def false_positives(self) -> list[ScenarioResult]:
        """Legitimate traffic refused contrary to what the scenario declared."""
        return [r for r in self.benign if r.blocked and not r.scenario.expect_blocked]

    def as_dict(self) -> dict[str, Any]:
        adversarial = self.adversarial
        benign = self.benign
        return {
            "generated_at": utcnow().isoformat(),
            "merchant": settings.MERCHANT_ID,
            "adversarial": {
                "total": len(adversarial),
                "blocked": len(self.blocked),
                "passed": len([r for r in adversarial if r.passed]),
                "missed": len(self.misses),
                "block_rate": round(len(self.blocked) / len(adversarial), 4)
                if adversarial
                else 0.0,
            },
            "benign": {
                "total": len(benign),
                "allowed": len([r for r in benign if not r.blocked]),
                "escalated_to_human": len(self.escalated),
                "false_positives": len(self.false_positives),
                "false_positive_rate": round(len(self.false_positives) / len(benign), 4)
                if benign
                else 0.0,
            },
            "families": sorted({r.scenario.family for r in adversarial}),
            "misses": [r.as_dict() for r in self.misses],
            "false_positive_detail": [r.as_dict() for r in self.false_positives],
            "results": [r.as_dict() for r in self.results],
        }


def run_corpus(session: Session, scenarios: list[Scenario] | None = None) -> CorpusReport:
    from app.catalog.policy_terms import ensure_active_terms
    from app.db.bootstrap import seed_catalog

    seed_catalog(session)
    ensure_active_terms(session)
    session.flush()

    results = [run_scenario(session, scenario) for scenario in (scenarios or load_all())]
    return CorpusReport(results=results)


def known_reason_codes() -> set[str]:
    return {code.value for code in ReasonCode}
