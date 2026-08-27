"""Dwarpal command line.

    python -m app.cli reports           regenerate the attack scorecard and dispute defence report
    python -m app.cli scorecard         the attack scorecard only
    python -m app.cli disputes          the dispute defence report only
    python -m app.cli export-evidence   write the evidence chain as JSONL for the offline verifier
    python -m app.cli export-jwks       write the merchant public JWK Set
    python -m app.cli verify-chain      in-process chain check
    python -m app.cli seed              create the schema and seed the catalog
    python -m app.cli check-channels    confirm the outbound channels are actually configured
    python -m app.cli probe-templates   send one message through each template, proving they work
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


def _bind(database: str):
    """Point this process at the named database, leaving its contents alone."""
    from app.db import base as db_base

    engine = _reports_engine(database)
    db_base.engine.dispose()
    db_base.engine = engine
    db_base.SessionFactory.configure(bind=engine)
    return engine


def _report_database(args: argparse.Namespace, suffix: str) -> str:
    """Each report gets its own database.

    They ran into one before, and every run starts by truncating, so generating both reports left
    only the second one's evidence chain behind. Keeping them apart means each chain survives its
    run and can be exported and verified.
    """
    base = args.database or settings.DB_NAME
    return f"{base}_{suffix}"


def _fresh_session(database: str):
    from app.db.bootstrap import create_schema

    engine = _bind(database)
    create_schema(engine)

    tables = [
        "buyer_run_events", "buyer_runs", "notification_log", "agent_connections",
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

    session = _fresh_session(_report_database(args, "reports"))
    try:
        report = run_corpus(session)
        session.commit()
    finally:
        session.close()

    paths = write_attack_scorecard(report)
    document = report.as_dict()
    adversarial, benign = document["adversarial"], document["benign"]
    print(
        f"attack scorecard: {adversarial['blocked']}/{adversarial['total']} blocked "
        f"across {adversarial['techniques']} techniques, {adversarial['missed']} missed, "
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

    session = _fresh_session(_report_database(args, "disputes"))
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

    # Without --database this reads the live chain. The reports run writes its packets to a
    # separate database, so exporting those means naming it.
    if args.database:
        _bind(args.database)
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


def cmd_verify_chain(args: argparse.Namespace) -> int:
    from app.db.base import session_scope
    from app.evidence.locker import verify_chain

    if args.database:
        _bind(args.database)
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


def cmd_check_channels(args: argparse.Namespace) -> int:
    """Ask the gateways whether they would work, without sending anything."""
    from app.channels import run

    del args
    report = run()
    for check in report.checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name}")
        print(f"         {check.detail}")
        if not check.ok and check.fix:
            print(f"         fix: {check.fix}")
    print()
    if report.failures:
        print(f"{len(report.failures)} of {len(report.checks)} checks failed")
        return 1
    print(f"all {len(report.checks)} checks passed")
    return 0


def cmd_probe_templates(args: argparse.Namespace) -> int:
    """Prove the template path delivers, by using it.

    Guarded the same way the scenario suite is: a real message reaches a real phone, so the caller
    has to say so explicitly rather than discovering it afterwards.
    """
    from app.template_probe import send_template_probe

    if not args.allow_live_whatsapp:
        print("This sends real WhatsApp messages to the configured recipient.")
        print("  Re-run with --allow-live-whatsapp if that is what you want.")
        return 2

    results = send_template_probe(to_number=args.to)
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        named = f"{result.template} ({result.language})" if result.template else "not configured"
        print(f"  [{mark}] {result.label} template")
        print(f"         {named}")
        if result.message_id:
            print(f"         {result.message_id}")
        if result.error:
            print(f"         {result.error}")
    failed = [r for r in results if not r.ok]
    print()
    print(f"{len(results) - len(failed)} of {len(results)} template sends accepted")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Dwarpal utilities.")
    parser.add_argument(
        "--database",
        default=None,
        help=(
            "database to act on, created if absent. Report generation defaults to "
            "<DB_NAME>_reports; export-evidence and verify-chain default to the live database."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reports", help="regenerate both reports").set_defaults(fn=cmd_reports)
    sub.add_parser("scorecard", help="attack scorecard only").set_defaults(fn=cmd_scorecard)
    sub.add_parser("disputes", help="dispute defence report only").set_defaults(fn=cmd_disputes)
    sub.add_parser("verify-chain", help="in-process evidence chain check").set_defaults(
        fn=cmd_verify_chain
    )
    sub.add_parser("seed", help="create the schema and seed the catalog").set_defaults(fn=cmd_seed)
    sub.add_parser(
        "check-channels", help="confirm Razorpay and WhatsApp are configured, sending nothing"
    ).set_defaults(fn=cmd_check_channels)

    probe = sub.add_parser(
        "probe-templates",
        help="send one message through each configured template and print the message ids",
    )
    probe.add_argument(
        "--allow-live-whatsapp",
        action="store_true",
        help="required, because this sends real messages to the configured recipient",
    )
    probe.add_argument("--to", default=None, help="override the recipient, E.164")
    probe.set_defaults(fn=cmd_probe_templates)

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
