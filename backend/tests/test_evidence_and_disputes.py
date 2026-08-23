"""The Evidence Locker, the standalone verifier, and the dispute responder."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.checkout import quote
from app.checkout.complete import complete
from app.disputes import responder
from app.evidence import locker
from app.harness import factory
from app.keys import merchant_jwks

BACKEND_ROOT = Path(__file__).resolve().parent.parent
VERIFIER = BACKEND_ROOT / "tools" / "verify_evidence.py"

CART = [("DWP-TEA-001", "Nilgiri Black Tea 250g", 1)]


def _transact(db, gateway, correlation: str) -> None:
    quoted = quote.create_quote(
        db,
        agent_id=f"agent:{correlation}",
        correlation_id=correlation,
        lines=[quote.RequestedLine(sku=sku, quantity=qty) for sku, _t, qty in CART],
    )
    principals = factory.Principals.create(agent_id=f"agent:{correlation}")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(CART),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        nonce=f"nonce-{correlation}",
    )
    complete(db, presentation.credentials, correlation_id=correlation, gateway=gateway)


def test_packets_are_chained_from_genesis(seeded, gateway):
    db = seeded
    for index in range(4):
        _transact(db, gateway, f"dwc_chain_{index}")
    db.commit()

    rows = locker.export_rows(db)
    assert len(rows) == 4
    assert rows[0]["prev_hash"] == locker.GENESIS_HASH
    for previous, current in itertools.pairwise(rows):
        assert current["prev_hash"] == previous["entry_hash"]
        assert current["seq"] == previous["seq"] + 1


def test_the_database_refuses_to_mutate_a_packet(seeded, gateway):
    db = seeded
    _transact(db, gateway, "dwc_immutable")
    db.commit()

    from app.db import base as db_base

    with db_base.engine.begin() as connection:
        for statement in (
            "UPDATE evidence_packets SET body = '{}'::jsonb",
            "DELETE FROM evidence_packets",
        ):
            try:
                connection.execute(text(statement))
                raise AssertionError(f"{statement} was permitted, so the locker is not append-only")
            except Exception as exc:
                assert "append-only" in str(exc)
                connection.rollback()
                break


def _export(db, tmp_path: Path) -> tuple[Path, Path]:
    rows = locker.export_rows(db)
    jsonl = tmp_path / "evidence.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(merchant_jwks()), encoding="utf-8")
    return jsonl, jwks


def _run_verifier(jsonl: Path, jwks: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), "--jsonl", str(jsonl), "--jwks", str(jwks), "--json"],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_ROOT),
        check=False,
    )


def test_standalone_verifier_validates_the_chain_from_stored_data(seeded, gateway, tmp_path):
    """The verifier must not need the application. It is run as a separate process."""
    db = seeded
    for index in range(3):
        _transact(db, gateway, f"dwc_verify_{index}")
    db.commit()

    jsonl, jwks = _export(db, tmp_path)
    result = _run_verifier(jsonl, jwks)
    assert result.returncode == 0, result.stdout + result.stderr

    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["packets"] == 3
    assert report["signatures_checked"] == 3
    assert report["problems"] == []


def test_standalone_verifier_detects_a_tampered_packet_body(seeded, gateway, tmp_path):
    db = seeded
    for index in range(3):
        _transact(db, gateway, f"dwc_tamper_{index}")
    db.commit()

    jsonl, jwks = _export(db, tmp_path)
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    rows[1]["body"]["outcome"] = "completed_but_actually_edited_later"
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = _run_verifier(jsonl, jwks)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["valid"] is False
    problems = {p["problem"] for p in report["problems"]}
    assert "packet_body_altered" in problems


def test_standalone_verifier_detects_a_tampered_body_with_a_recomputed_hash(
    seeded, gateway, tmp_path
):
    """The harder attack: edit the body and rewrite the stored hash so it looks self-consistent.

    Recomputing the entry hash defeats the body check, but the hash is what the merchant signed
    and what the next packet commits to, so the signature and the chain link both break instead.
    """
    from app.ap2.jose import canonical_json, sha256_b64url

    db = seeded
    for index in range(3):
        _transact(db, gateway, f"dwc_recomputed_{index}")
    db.commit()

    jsonl, jwks = _export(db, tmp_path)
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    rows[1]["body"]["outcome"] = "quietly_rewritten"
    rows[1]["entry_hash"] = sha256_b64url(
        canonical_json(
            {
                "seq": rows[1]["seq"],
                "correlation_id": rows[1]["correlation_id"],
                "prev_hash": rows[1]["prev_hash"],
                "body": rows[1]["body"],
                "created_at": rows[1]["created_at"],
            }
        )
    )
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = _run_verifier(jsonl, jwks)
    assert result.returncode == 1
    problems = {p["problem"] for p in json.loads(result.stdout)["problems"]}
    assert "signature_covers_a_different_entry" in problems
    assert "broken_chain_link" in problems


def test_standalone_verifier_detects_a_removed_packet(seeded, gateway, tmp_path):
    db = seeded
    for index in range(3):
        _transact(db, gateway, f"dwc_removed_{index}")
    db.commit()

    jsonl, jwks = _export(db, tmp_path)
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    del rows[1]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = _run_verifier(jsonl, jwks)
    assert result.returncode == 1
    problems = {p["problem"] for p in json.loads(result.stdout)["problems"]}
    assert "sequence_gap" in problems or "broken_chain_link" in problems


def test_standalone_verifier_detects_a_forged_signature(seeded, gateway, tmp_path):
    db = seeded
    _transact(db, gateway, "dwc_forge")
    db.commit()

    jsonl, jwks = _export(db, tmp_path)
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    head, payload, signature = rows[0]["signature"].split(".")
    flipped = "A" * len(signature) if not signature.startswith("A") else "B" * len(signature)
    rows[0]["signature"] = f"{head}.{payload}.{flipped}"
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = _run_verifier(jsonl, jwks)
    assert result.returncode == 1
    problems = {p["problem"] for p in json.loads(result.stdout)["problems"]}
    assert "signature_invalid" in problems


def test_the_verifier_imports_nothing_from_the_application() -> None:
    """If it needed the running application to pass, it would prove nothing."""
    source = VERIFIER.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "app." not in stripped and " app" not in f" {stripped}", (
                f"the standalone verifier must not import from the application: {stripped}"
            )


def test_evidence_snapshot_can_reconstruct_what_the_buyer_was_shown(seeded, gateway):
    db = seeded
    _transact(db, gateway, "dwc_snapshot")
    db.commit()

    body = locker.for_correlation(db, "dwc_snapshot")[0].body
    snapshot = body["checkout"]["catalog_snapshot"]
    assert snapshot
    entry = snapshot[0]
    assert entry["sku"] == "DWP-TEA-001"
    assert entry["price_minor"] > 0
    assert "purchase_constraints" in entry
    assert entry["observed_at"]


def test_strong_evidence_is_recommended_for_contest(seeded, gateway):
    db = seeded
    _transact(db, gateway, "dwc_strong")
    db.commit()

    row = responder.respond(db, correlation_id="dwc_strong", claim="not authorised")
    assert row.recommendation == responder.Recommendation.CONTEST.value
    assert row.strength_score >= responder.CONTEST_THRESHOLD
    assert row.representment["narrative"]
    assert row.representment["timeline"]


def test_absent_evidence_is_recommended_for_refund(seeded):
    db = seeded
    row = responder.respond(db, correlation_id="dwc_nothing_here", claim="not authorised")
    assert row.recommendation == responder.Recommendation.REFUND.value
    assert row.strength_score == 0
    assert "no evidence packet exists for this transaction" in row.representment["weaknesses"]


def test_the_responder_does_not_contest_everything(seeded, gateway):
    """A responder that recommends contesting every dispute is worthless."""
    db = seeded
    _transact(db, gateway, "dwc_mixed_strong")
    db.commit()

    strong = responder.respond(db, correlation_id="dwc_mixed_strong", claim="not authorised")
    weak = responder.respond(db, correlation_id="dwc_no_such_transaction", claim="not authorised")

    recommendations = {strong.recommendation, weak.recommendation}
    assert recommendations == {"contest", "refund"}


def test_a_compensated_transaction_is_never_contested():
    body = {
        "outcome": "compensated",
        "credential_chain": {"open_checkout_mandate": "x"},
        "verification": {"steps_passed": list("abcdefg")},
        "checkout": {"policy_hash": "h", "catalog_snapshot": [{}]},
        "verdicts": [{"decision": "allow", "signed_jwt": "a.b.c"}],
        "payments": [{"razorpay_payment_id": "pay_1"}],
        "escalations": [],
        "semantic_checks": [],
    }
    representment = responder.build_representment(body, correlation_id="dwc_comp")
    assert representment.recommendation is responder.Recommendation.REFUND
    assert representment.strength_score == 0
    assert any("already refunded" in w for w in representment.weaknesses)


def test_evidence_is_filed_for_refusals_as_well(seeded, gateway):
    db = seeded
    quoted = quote.create_quote(
        db,
        agent_id="agent:refused",
        correlation_id="dwc_refused_evidence",
        lines=[quote.RequestedLine(sku="DWP-TEA-001", quantity=1)],
    )
    principals = factory.Principals.create(agent_id="agent:refused")
    presentation = factory.present(
        principals,
        factory.spec_for_cart(CART),
        checkout_jwt=quoted.checkout_jwt,
        checkout_hash=quoted.checkout_hash,
        amount_minor=quoted.row.total_minor,
        tamper=factory.Tamper(expired=True),
    )
    complete(db, presentation.credentials, correlation_id="dwc_refused_evidence", gateway=gateway)
    db.commit()

    packets = locker.for_correlation(db, "dwc_refused_evidence")
    assert len(packets) == 1
    assert packets[0].body["outcome"] == "refused_verification"
    assert packets[0].body["verification"]["reason_code"] == "CRED_EXPIRED"
