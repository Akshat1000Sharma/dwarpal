"""The adversarial corpus, executed against the real gate.

Scenarios are data. Every file under corpus/ is parametrised into this module, so adding an attack
family needs no test code. Blocks and false positives are asserted together, because a gate that
refuses everything would otherwise look perfect.

A technique can declare a matrix, and the corpus then executes it against several items, tiers and
amounts. The fast suite runs one case per technique, because the ``seeded`` fixture truncates and
re-seeds the database for each one and the full matrix would make ``pytest`` unusable. The full
matrix is executed by ``python -m app.cli reports``, which is what the scorecard and the landing
page quote, and is available here as ``pytest -m corpus_matrix``.
"""

from __future__ import annotations

import pytest

from app.harness import runner


def _scenarios():
    return runner.load_all()


def _representative():
    return runner.one_per_technique(_scenarios())


def test_the_corpus_covers_a_serious_range_of_techniques():
    adversarial = runner.load_scenarios(runner.ADVERSARIAL_DIR, "adversarial")
    benign = runner.load_scenarios(runner.BENIGN_DIR, "benign")
    techniques = {s.technique for s in adversarial}
    assert len(techniques) >= 40, "the adversarial corpus should cover at least forty techniques"
    assert len(adversarial) >= 400, "each technique should be executed against a range of inputs"
    assert benign, "a matched benign corpus is required"
    assert len(benign) >= 100, (
        "the benign corpus has to be comparable in size, or the false-positive rate is decorative"
    )


def test_every_declared_reason_code_exists():
    known = runner.known_reason_codes()
    for scenario in _scenarios():
        unknown = [c for c in scenario.expect_reason_codes if c not in known]
        assert not unknown, f"{scenario.id} expects codes outside the closed set: {unknown}"


def test_scenario_ids_are_unique():
    ids = [s.id for s in _scenarios()]
    assert len(ids) == len(set(ids))


def test_generated_cases_are_genuinely_different():
    """A matrix must produce distinct executions, not the same case under many names.

    This is what stops the corpus being padded. Two cases that send identical credentials, cart,
    mandate and setup prove exactly one thing between them, however they are counted.
    """
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for scenario in _scenarios():
        key = f"{scenario.kind}:{scenario.shape}"
        if key in seen:
            duplicates.append(f"{scenario.id} is identical to {seen[key]}")
        else:
            seen[key] = scenario.id
    assert not duplicates, "duplicate cases inflate the count without proving anything:\n" + "\n".join(
        duplicates
    )


@pytest.mark.parametrize("scenario", _representative(), ids=lambda s: s.technique)
def test_technique(seeded, scenario):
    result = runner.run_scenario(seeded, scenario)
    assert result.passed, (
        f"{scenario.id} ({scenario.family}): expected blocked={scenario.expect_blocked} "
        f"reason in {scenario.expect_reason_codes or 'any'}, observed blocked={result.blocked} "
        f"reason={result.reason_code} status={result.status}"
    )


@pytest.mark.corpus_matrix
@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.id)
def test_every_case_in_the_matrix(seeded, scenario):
    result = runner.run_scenario(seeded, scenario)
    assert result.passed, (
        f"{scenario.id} ({scenario.family}): expected blocked={scenario.expect_blocked} "
        f"reason in {scenario.expect_reason_codes or 'any'}, observed blocked={result.blocked} "
        f"reason={result.reason_code} status={result.status}"
    )
