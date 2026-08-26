"""The scenario suites, in the order they run.

Order matters only in that the cheap structural checks come first, so a badly configured merchant
fails in seconds rather than after the soak. Otherwise each suite is independent and creates its
own agents, mandates and carts.
"""

from __future__ import annotations

from collections.abc import Callable

from scenarios.harness import Context, Suite
from scenarios.suites import (
    s01_purchase_lifecycle,
    s02_credential_attacks,
    s03_budget_concurrency,
    s04_inventory_contention,
    s05_structuring_velocity,
    s06_revocation_races,
    s07_escalation_and_semantics,
    s08_idempotency_and_webhooks,
    s09_evidence_and_disputes,
    s10_degraded_unverified,
    s11_soak_mixed_traffic,
    s12_human_present,
)

ALL: list[Callable[[Context], Suite]] = [
    s01_purchase_lifecycle.run,
    s02_credential_attacks.run,
    s03_budget_concurrency.run,
    s04_inventory_contention.run,
    s05_structuring_velocity.run,
    s06_revocation_races.run,
    s07_escalation_and_semantics.run,
    s08_idempotency_and_webhooks.run,
    s09_evidence_and_disputes.run,
    s10_degraded_unverified.run,
    s11_soak_mixed_traffic.run,
    s12_human_present.run,
]

# The smoke profile is a configuration check, not a proof. It runs the structural suites only.
SMOKE = {"s01", "s02", "s10"}
