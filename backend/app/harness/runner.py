"""Adversarial and benign corpus runner.

Scenarios are data, not hand-written test functions, so a new attack can be added by dropping a
YAML file in without touching any code. The runner turns each scenario into a real checkout
attempt against the real gate: the same verification pipeline, the same kernel, the same evidence
locker. Nothing here targets any external system; every request is fired at Dwarpal's own door.

The benign corpus runs alongside the adversarial one because a gate that blocks everything scores
perfectly against attacks and is useless. Both numbers are always reported together.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.catalog import policy_terms
from app.checkout import quote
from app.checkout.complete import complete
from app.db.base import utcnow
from app.db.bootstrap import seed_catalog
from app.db.models import (
    CheckoutSession,
    Escalation,
    HoldStatus,
    InventoryHold,
    PaymentStatus,
    Product,
)
from app.escalation import service as escalation_service
from app.escalation.whatsapp import RecordingTransport
from app.harness import factory
from app.kernel import revocation
from app.kernel.reasons import ReasonCode
from app.payments.gateway import StubGateway
from app.semantic.client import KeywordSemanticClient
from app.settings import settings
from app.trust import registry as trust_registry

CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"
ADVERSARIAL_DIR = CORPUS_ROOT / "adversarial"
BENIGN_DIR = CORPUS_ROOT / "benign"


@dataclass(frozen=True)
class Scenario:
    """One declarative case: one technique, executed against one specific set of inputs."""

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
    technique: str = ""

    @property
    def cart_lines(self) -> list[tuple[str, str, int]]:
        return [(c["sku"], c.get("title", c["sku"]), int(c.get("quantity", 1))) for c in self.cart]

    @property
    def handle(self) -> str:
        """A short, stable identifier for the records this case writes.

        The readable id is what the report names, and a matrix makes it long. Correlation
        identifiers are a fixed-width column, so what goes in the database is the technique plus a
        digest of the full id: still traceable back to one case, and never truncated.
        """
        digest = hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:8]
        return f"{(self.technique or self.id)[:30]}-{digest}"

    @property
    def shape(self) -> str:
        """What this case actually sends, so two cases differing only in name are detectable."""
        return json.dumps(
            {
                "cart": self.cart,
                "mandate": self.mandate,
                "tamper": self.tamper,
                "setup": self.setup,
            },
            sort_keys=True,
            default=str,
        )


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
            "technique": self.scenario.technique or self.scenario.id,
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


# A matrix is the cartesian product of its dimensions, so a careless one grows without anybody
# noticing. Past this, loading fails rather than turning a report run into a forty-minute wait.
MATRIX_CASE_LIMIT = 120


def _slug(value: Any) -> str:
    """A short, stable, readable name for one matrix value, so a miss names something actionable."""
    if isinstance(value, list):
        parts = []
        for entry in value:
            if not isinstance(entry, dict):
                parts.append(_slug(entry))
                continue
            sku = str(entry.get("sku", "")).replace("DWP-", "").lower()
            quantity = int(entry.get("quantity", 1))
            parts.append(sku if quantity == 1 else f"{sku}x{quantity}")
        return "+".join(parts) or "empty"
    if isinstance(value, dict):
        canonical = json.dumps(value, sort_keys=True, default=str).encode()
        return hashlib.sha256(canonical).hexdigest()[:8]
    text = str(value).lower()
    # An issuer is a DID; its host label is the part a reader recognises.
    if text.startswith("did:web:"):
        text = text.split(":")[-1].split(".")[0]
    cleaned = "".join(character if character.isalnum() else "-" for character in text)
    return cleaned.strip("-") or "none"


MERGE_DIMENSION = "merge"


def _set_path(entry: dict[str, Any], path: str, value: Any) -> None:
    """Assign through a dotted key, so a matrix can address mandate.issuer_id and setup.*."""
    keys = path.split(".")
    cursor = entry
    for key in keys[:-1]:
        nested = cursor.get(key)
        nested = dict(nested) if isinstance(nested, dict) else {}
        cursor[key] = nested
        cursor = nested
    cursor[keys[-1]] = value


def _merge(entry: dict[str, Any], patch: dict[str, Any]) -> None:
    """Fold a whole mapping into the case.

    Dimensions are combined as a cartesian product, which is wrong when two fields have to move
    together: a cart and the cap that equals its total exactly are one choice, not two. The merge
    dimension expresses that choice as a single value.
    """
    for key, value in patch.items():
        existing = entry.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = dict(existing)
            _merge(merged, value)
            entry[key] = merged
        else:
            entry[key] = deepcopy(value)


def expand(entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Turn one declared technique into the concrete cases it stands for.

    Without a matrix a technique is a single case, which is how the corpus started. With one, every
    combination is a separate execution against the real gate: a different item, tier, cap or
    quantity reaches different code, so each case can fail on its own.
    """
    base_id = str(entry["id"])
    matrix = entry.get("matrix") or {}
    if not matrix:
        return [(base_id, entry)]

    dimensions: list[tuple[str, list[tuple[str, Any]]]] = []
    for path, values in matrix.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"{base_id}: matrix dimension {path!r} must be a non-empty list")
        labelled: list[tuple[str, Any]] = []
        for value in values:
            if isinstance(value, dict) and set(value) == {"label", "value"}:
                labelled.append((str(value["label"]), value["value"]))
            else:
                labelled.append((_slug(value), value))
        dimensions.append((str(path), labelled))

    combinations = list(itertools.product(*(values for _path, values in dimensions)))
    if len(combinations) > MATRIX_CASE_LIMIT:
        raise ValueError(
            f"{base_id}: matrix expands to {len(combinations)} cases, over the "
            f"{MATRIX_CASE_LIMIT} limit. Split the technique rather than raising the limit."
        )

    cases: list[tuple[str, dict[str, Any]]] = []
    for combination in combinations:
        case = deepcopy(entry)
        case.pop("matrix", None)
        for (path, _values), (_label, value) in zip(dimensions, combination, strict=True):
            if path == MERGE_DIMENSION:
                if not isinstance(value, dict):
                    raise ValueError(f"{base_id}: a {MERGE_DIMENSION} value must be a mapping")
                _merge(case, value)
            else:
                _set_path(case, path, deepcopy(value))
        slug = "/".join(label for label, _value in combination)
        cases.append((f"{base_id}/{slug}", case))
    return cases


def load_scenarios(directory: Path, kind: str) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        family = str(document.get("family", path.stem))
        for declared in document.get("scenarios", []) or []:
            technique = str(declared["id"])
            for case_id, entry in expand(declared):
                scenarios.append(
                    Scenario(
                        id=case_id,
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
                        technique=technique,
                    )
                )
    return scenarios


def load_all() -> list[Scenario]:
    return [
        *load_scenarios(ADVERSARIAL_DIR, "adversarial"),
        *load_scenarios(BENIGN_DIR, "benign"),
    ]


def one_per_technique(scenarios: list[Scenario]) -> list[Scenario]:
    """The first case of each technique.

    The fast suite runs this rather than the full matrix: it proves every technique is defended, at
    a size that keeps pytest usable. The full matrix is what the scorecard executes.
    """
    seen: set[tuple[str, str]] = set()
    representative: list[Scenario] = []
    for scenario in scenarios:
        key = (scenario.kind, scenario.technique or scenario.id)
        if key in seen:
            continue
        seen.add(key)
        representative.append(scenario)
    return representative


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
    if mandate.get("amount_min_minor") is not None:
        spec.amount_min_minor = int(mandate["amount_min_minor"])
    if mandate.get("allowed_pisps") is not None:
        spec.allowed_pisps = list(mandate["allowed_pisps"])
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


def reset_between_cases(session: Session) -> None:
    """Put the merchant back to its opening state before a case runs.

    Every case quotes, and a quote holds stock for INVENTORY_HOLD_TTL_SECONDS. The corpus runs in
    one session, so nothing expires mid-run and a few hundred cases would empty the shelves; the
    sold-out refusals that follow are correct behaviour reported as scorecard misses. The mock
    authorities also mint a key per case, and an unbounded key set makes every forged credential
    cost a scan of all of them.

    Resetting here also makes each case independent of the order the corpus happens to run in.
    """
    session.execute(
        update(InventoryHold)
        .where(InventoryHold.status == HoldStatus.HELD)
        .values(status=HoldStatus.RELEASED)
    )
    seed_catalog(session, replace=True)
    trust_registry.reset_cache()
    session.flush()


def run_scenario(
    session: Session,
    scenario: Scenario,
    *,
    gateway: StubGateway | None = None,
) -> ScenarioResult:
    """Execute one scenario against the real gate and compare with what it declared."""
    gateway = gateway or StubGateway()
    reset_between_cases(session)
    correlation = f"corpus_{scenario.handle}"
    issuer = scenario.mandate.get("issuer_id", factory.DEFAULT_ISSUER)
    principals = factory.Principals.create(
        issuer_id=issuer, agent_id=f"agent:{scenario.handle}", register=True
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

        if setup.get("quote_only"):
            # The point of a hold-exhaustion attack is holds that are never converted. Completing
            # would consume or release them, which frees the quota and makes the technique
            # unreachable, so these attempts stop at the quote.
            last = ScenarioResult(
                scenario=scenario,
                blocked=False,
                reason_code=None,
                status="held_at_quote",
                passed=False,
                note="stock held and not converted",
            )
            last.passed = _matches(scenario, last)
            continue

        if setup.get("policy_changes_after_quote"):
            original = policy_terms.read_terms_file
            # The revision is unique per case. A fixed body meant the second case to run this
            # setup revised the terms to what they already said, so the hash never moved and
            # the attack quietly stopped being one.
            revised = f"# Revised terms\n\nThe terms changed for {scenario.handle}.\n"
            policy_terms.read_terms_file = lambda body=revised: body
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
            nonce=f"nonce-{scenario.handle}-{attempt}",
            human_present=bool(setup.get("human_present")),
        )

        if setup.get("expire_quote"):
            quoted.row.expires_at = utcnow() - timedelta(seconds=1)
            session.flush()

        if setup.get("revoke_before_capture"):
            _revoke_mandate(session, presentation)

        if setup.get("revoke_between_authorisation_and_capture"):
            outcome = _settle_with_revocation_in_flight(
                session, presentation, f"{correlation}_{attempt}", gateway
            )
        else:
            outcome = complete(
                session,
                presentation.credentials,
                correlation_id=f"{correlation}_{attempt}",
                gateway=gateway,
                semantic_client=KeywordSemanticClient(),
                whatsapp=RecordingTransport(),
                audience=settings.PUBLIC_BASE_URL,
                buyer_region=setup.get("buyer_region"),
            )

        # Revoking after this attempt settles makes the next attempt the mandate's next use, which
        # is the case being scored. The mandate comes from the settled checkout because the
        # credentials are spent by now and would fail replay before revocation was reached.
        if setup.get("revoke_after_capture") and attempt == 0:
            settled = session.get(CheckoutSession, outcome.checkout_id)
            if settled is not None and settled.mandate_id:
                revocation.revoke(session, settled.mandate_id, "revoked by the corpus")
                session.flush()

        answer = setup.get("answer_escalation")
        if answer and outcome.status == "escalated":
            # The deadline is moved rather than waited out. An escalation nobody answers has to
            # settle as a denial, and the only way to observe that is to present again afterwards
            # over the same Checkout, which is what resettle_same_checkout then does.
            escalation = session.get(Escalation, outcome.detail["escalation_id"])
            if escalation is not None:
                if answer == "timeout":
                    escalation.deadline_at = utcnow() - timedelta(seconds=1)
                else:
                    escalation_service.record_answer(session, escalation.id, str(answer))
                session.flush()

        if setup.get("resettle_same_checkout"):
            # Fresh closed mandates over the same merchant-signed Checkout. They are not replays,
            # so nothing in the credential chain refuses them; only the Checkout's own state does.
            resettled = factory.present_issued(
                issued,
                checkout_jwt=quoted.checkout_jwt,
                checkout_hash=quoted.checkout_hash,
                amount_minor=quoted.row.total_minor,
                tamper=tamper,
                nonce=f"nonce-{scenario.handle}-{attempt}-resettle",
                human_present=bool(setup.get("human_present")),
            )
            outcome = complete(
                session,
                resettled.credentials,
                correlation_id=f"{correlation}_{attempt}_resettle",
                gateway=gateway,
                semantic_client=KeywordSemanticClient(),
                whatsapp=RecordingTransport(),
                audience=settings.PUBLIC_BASE_URL,
            )

        if setup.get("replay_same_credentials"):
            outcome = complete(
                session,
                presentation.credentials,
                correlation_id=f"{correlation}_{attempt}_replay",
                gateway=gateway,
                semantic_client=KeywordSemanticClient(),
                whatsapp=RecordingTransport(),
                audience=settings.PUBLIC_BASE_URL,
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


def _settle_with_revocation_in_flight(
    session: Session,
    presentation: factory.Presentation,
    correlation: str,
    gateway: StubGateway,
) -> Any:
    """Revoke after the order is authorised but before capture is confirmed.

    This is the window a live Razorpay flow actually has: the order exists and the money is
    authorised, and the capture webhook arrives in a later request. A revocation landing in that
    window must be compensated rather than settled.
    """
    from app.checkout import complete as complete_module
    from app.db.models import Payment

    original = complete_module._authorize
    complete_module._authorize = lambda *_a, **_k: None
    try:
        outcome = complete(
            session,
            presentation.credentials,
            correlation_id=correlation,
            gateway=gateway,
            semantic_client=KeywordSemanticClient(),
            whatsapp=RecordingTransport(),
            audience=settings.PUBLIC_BASE_URL,
        )
    finally:
        complete_module._authorize = original

    if outcome.status != "awaiting_payment" or outcome.payment_id is None:
        return outcome

    awaiting = session.get(CheckoutSession, outcome.checkout_id)
    revocation.revoke(session, awaiting.mandate_id, "revoked by the corpus")
    session.flush()
    payment = session.get(Payment, outcome.payment_id)
    payment.razorpay_payment_id = f"pay_{correlation}"
    payment.status = PaymentStatus.CAPTURED
    payment.captured_at = utcnow()
    session.flush()
    complete_module.finalise_captured(session, payment, gateway=gateway)
    session.flush()

    row = session.get(CheckoutSession, outcome.checkout_id)
    if row is None:
        return outcome
    # The settling call carries its own reason: the mandate died between authorisation and capture.
    return replace(outcome, status=row.state, reason_code=ReasonCode.MANDATE_REVOKED)


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

    @property
    def settled_without_asking(self) -> list[ScenarioResult]:
        """Benign traffic declared to need a human that the merchant completed on its own.

        The mirror image of a false positive, and the more dangerous one. It is neither refused
        nor escalated, so without its own line it would be counted as a clean approval.
        """
        return [r for r in self.benign if r.scenario.expect_blocked and not r.blocked]

    @staticmethod
    def _techniques(results: list[ScenarioResult]) -> list[str]:
        return sorted({r.scenario.technique or r.scenario.id for r in results})

    def by_technique(self) -> list[dict[str, Any]]:
        """Cases, blocks and misses rolled up per technique.

        A technique is one attack idea; a case is that idea executed against one item, tier and
        amount. Reporting them separately is what stops a large case count reading as a claim
        about how many distinct attacks were tried.
        """
        rollup: dict[str, dict[str, Any]] = {}
        for result in self.adversarial:
            name = result.scenario.technique or result.scenario.id
            entry = rollup.setdefault(
                name,
                {"technique": name, "family": result.scenario.family, "cases": 0, "blocked": 0,
                 "passed": 0, "missed": 0},
            )
            entry["cases"] += 1
            entry["blocked"] += int(result.blocked)
            entry["passed"] += int(result.passed)
            entry["missed"] += int(not result.passed)
        return [rollup[name] for name in sorted(rollup)]

    def as_dict(self) -> dict[str, Any]:
        adversarial = self.adversarial
        benign = self.benign
        return {
            "generated_at": utcnow().isoformat(),
            "merchant": settings.MERCHANT_ID,
            "adversarial": {
                "total": len(adversarial),
                "techniques": len(self._techniques(adversarial)),
                "blocked": len(self.blocked),
                "passed": len([r for r in adversarial if r.passed]),
                "missed": len(self.misses),
                "block_rate": round(len(self.blocked) / len(adversarial), 4)
                if adversarial
                else 0.0,
            },
            "benign": {
                "total": len(benign),
                "techniques": len(self._techniques(benign)),
                "allowed": len([r for r in benign if not r.blocked]),
                "escalated_to_human": len(self.escalated),
                "false_positives": len(self.false_positives),
                "settled_without_asking": len(self.settled_without_asking),
                "false_positive_rate": round(len(self.false_positives) / len(benign), 4)
                if benign
                else 0.0,
            },
            "families": sorted({r.scenario.family for r in adversarial}),
            "techniques": self._techniques(adversarial),
            "by_technique": self.by_technique(),
            "misses": [r.as_dict() for r in self.misses],
            "false_positive_detail": [r.as_dict() for r in self.false_positives],
            "settled_without_asking_detail": [r.as_dict() for r in self.settled_without_asking],
            "results": [r.as_dict() for r in self.results],
        }


def run_corpus(session: Session, scenarios: list[Scenario] | None = None) -> CorpusReport:
    from app.catalog.policy_terms import ensure_active_terms

    seed_catalog(session)
    ensure_active_terms(session)
    session.flush()

    results = [run_scenario(session, scenario) for scenario in (scenarios or load_all())]
    return CorpusReport(results=results)


def known_reason_codes() -> set[str]:
    return {code.value for code in ReasonCode}
