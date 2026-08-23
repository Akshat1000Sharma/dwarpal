"""Dwarpal command line.

    python -m app.cli reports           regenerate the attack scorecard and dispute defence report
    python -m app.cli scorecard         the attack scorecard only
    python -m app.cli disputes          the dispute defence report only
    python -m app.cli export-evidence   write the evidence chain as JSONL for the offline verifier
    python -m app.cli export-jwks       write the merchant public JWK Set
    python -m app.cli verify-chain      in-process chain check
    python -m app.cli seed              create the schema and seed the catalog
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.settings import settings


def _reports_engine(database: str):
    """A dedicated database for report generation, so a run never disturbs live data."""
    admin = create_engine(settings.maintenance_database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": database}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()

    url = settings.database_url.rsplit("/", 1)[0] + f"/{database}"
    return create_engine(url, pool_pre_ping=True, future=True)


def _fresh_session(database: str):
    from app.db import base as db_base
    from app.db.bootstrap import create_schema

    engine = _reports_engine(database)
    db_base.engine.dispose()
    db_base.engine = engine
    db_base.SessionFactory.configure(bind=engine)
    create_schema(engine)

    tables = [
        "evidence_packets", "escalation_responses", "escalations", "refunds",
        "payment_exceptions", "payments", "verdicts", "spend_events",
        "budget_reservations", "inventory_holds", "credential_nonces",
        "idempotency_keys", "checkout_sessions", "open_mandates",
        "agent_identities", "disputes", "products", "policy_terms",
    ]
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE " + ", ".join(tables) + " RESTART IDENTITY"))
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def cmd_scorecard(args: argparse.Namespace) -> int:
    from app.harness.runner import run_corpus
    from app.harness.scorecard import write_attack_scorecard

    session = _fresh_session(args.database)
    try:
        report = run_corpus(session)
        session.commit()
    finally:
        session.close()

    paths = write_attack_scorecard(report)
    document = report.as_dict()
    adversarial, benign = document["adversarial"], document["benign"]
    print(
        f"attack scorecard: {adversarial['blocked']}/{adversarial['total']} blocked, "
        f"{adversarial['missed']} missed, "
        f"{benign['false_positives']}/{benign['total']} false positives"
    )
    for miss in document["misses"]:
        print(f"  MISS {miss['id']}: observed {miss['observed_reason_code']}")
    for entry in document["false_positive_detail"]:
        print(f"  FALSE POSITIVE {entry['id']}: {entry['observed_reason_code']}")
    print(f"  written to {paths['json']} and {paths['markdown']}")
    return 0 if not document["misses"] and not document["false_positive_detail"] else 1


def cmd_disputes(args: argparse.Namespace) -> int:
    from app.harness.disputes import run_batch
    from app.harness.scorecard import write_dispute_report

    session = _fresh_session(args.database)
    try:
        document = run_batch(session)
        session.commit()
    finally:
        session.close()

    paths = write_dispute_report(document)
    print(
        f"dispute defence: {document['with_evidence']['defensible']}/{document['total']} "
        f"defensible with evidence, {document['baseline']['defensible']}/{document['total']} "
        f"without, improvement {document['improvement'] * 100:.1f}%"
    )
    for entry in document["refund_recommended"]:
        print(f"  REFUND RECOMMENDED {entry['case_id']} (score {entry['strength_score']})")
    print(f"  written to {paths['json']} and {paths['markdown']}")
    return 0


def cmd_reports(args: argparse.Namespace) -> int:
    first = cmd_scorecard(args)
    second = cmd_disputes(args)
    return first or second


def cmd_export_evidence(args: argparse.Namespace) -> int:
    from app.db.base import session_scope
    from app.evidence.locker import export_rows

    with session_scope() as session:
        rows = export_rows(session)
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    print(f"exported {len(rows)} evidence packets to {destination}")
    return 0


def cmd_export_jwks(args: argparse.Namespace) -> int:
    from app.keys import merchant_jwks

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(merchant_jwks(), indent=2), encoding="utf-8")
    print(f"wrote the merchant public JWK Set to {destination}")
    return 0


def cmd_verify_chain(_: argparse.Namespace) -> int:
    from app.db.base import session_scope
    from app.evidence.locker import verify_chain

    with session_scope() as session:
        report = verify_chain(session)
    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


def cmd_seed(_: argparse.Namespace) -> int:
    from app.catalog.policy_terms import ensure_active_terms
    from app.db.base import session_scope
    from app.db.bootstrap import create_schema, seed_catalog

    create_schema()
    with session_scope() as session:
        seeded = seed_catalog(session)
        terms = ensure_active_terms(session)
    print(f"seeded {seeded} products, policy hash {terms.content_hash}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Dwarpal utilities.")
    parser.add_argument(
        "--database",
        default=f"{settings.DB_NAME}_reports",
        help="database used for report generation, created if absent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reports", help="regenerate both reports").set_defaults(fn=cmd_reports)
    sub.add_parser("scorecard", help="attack scorecard only").set_defaults(fn=cmd_scorecard)
    sub.add_parser("disputes", help="dispute defence report only").set_defaults(fn=cmd_disputes)
    sub.add_parser("verify-chain", help="in-process evidence chain check").set_defaults(
        fn=cmd_verify_chain
    )
    sub.add_parser("seed", help="create the schema and seed the catalog").set_defaults(fn=cmd_seed)

    export = sub.add_parser("export-evidence", help="write the evidence chain as JSONL")
    export.add_argument("--out", default="./reports/evidence.jsonl")
    export.set_defaults(fn=cmd_export_evidence)

    jwks = sub.add_parser("export-jwks", help="write the merchant public JWK Set")
    jwks.add_argument("--out", default="./reports/merchant_jwks.json")
    jwks.set_defaults(fn=cmd_export_jwks)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
