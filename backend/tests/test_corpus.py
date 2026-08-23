"""The adversarial corpus, executed against the real gate.

Scenarios are data. Every file under corpus/ is parametrised into this module, so adding an attack
family needs no test code. Blocks and false positives are asserted together, because a gate that
refuses everything would otherwise look perfect.
"""

from __future__ import annotations

import pytest

from app.harness import runner


def _scenarios():
    return runner.load_all()


def test_the_corpus_is_not_empty():
    adversarial = runner.load_scenarios(runner.ADVERSARIAL_DIR, "adversarial")
    benign = runner.load_scenarios(runner.BENIGN_DIR, "benign")
    assert len(adversarial) >= 18, "the specification names eighteen attack families"
    assert benign, "a matched benign corpus is required"


def test_every_declared_reason_code_exists():
    known = runner.known_reason_codes()
    for scenario in _scenarios():
        unknown = [c for c in scenario.expect_reason_codes if c not in known]
        assert not unknown, f"{scenario.id} expects codes outside the closed set: {unknown}"


def test_scenario_ids_are_unique():
    ids = [s.id for s in _scenarios()]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s.id)
def test_scenario(seeded, scenario):
    result = runner.run_scenario(seeded, scenario)
    assert result.passed, (
        f"{scenario.id} ({scenario.family}): expected blocked={scenario.expect_blocked} "
        f"reason in {scenario.expect_reason_codes or 'any'}, observed blocked={result.blocked} "
        f"reason={result.reason_code} status={result.status}"
    )
