"""Test fixtures.

PostgreSQL is required, not optional. The concurrency guarantees rest on real row-level locking,
so a suite that silently skipped them against a weaker store would be dishonest. If the database
is unreachable the suite fails immediately with an instruction, rather than passing with the most
important tests quietly skipped.

The database name is forced to a ``_test`` suffix, so running the suite can never touch a
development database.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# Settings the suite decides, whatever a developer's .env or the shell says. These are not
# preferences: APP_ENV=testing is what selects the recording WhatsApp transport, the deterministic
# buyer planner and the stub payment gateway, so a local .env carrying APP_ENV=development would
# quietly point the suite at Meta, Gemini and Razorpay. The signing key directory is forced for the
# same reason, so a test run cannot mint keys into the directory a real deployment is using.
FORCED = {
    "APP_ENV": "testing",
    "MERCHANT_SIGNING_KEY_DIR": "./secrets/merchant_keys_test",
    "META_TEMPLATE_NAME": "",
    "META_RECEIPT_TEMPLATE_NAME": "",
}


def _bootstrap_environment() -> None:
    """Fill in placeholders before any application module reads configuration."""
    from dotenv import load_dotenv

    load_dotenv(BACKEND_ROOT / ".env", override=False)

    defaults = {
        "LOG_LEVEL": "WARNING",
        "SECRET_KEY": "test-secret-key-for-local-and-ci-use-only",
        "PUBLIC_BASE_URL": "http://localhost:8000",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "dwarpal",
        "DB_USER": "dwarpal",
        "DB_PASSWORD": "dwarpal",
        # Placeholders only. No test may require a real credential.
        "RAZORPAY_KEY_ID": "rzp_test_ci",
        "RAZORPAY_KEY_SECRET": "ci-secret",
        "RAZORPAY_WEBHOOK_SECRET": "ci-webhook-secret",
        "GEMINI_API_KEY": "ci-gemini-key",
        "META_VERIFY_TOKEN": "ci-verify-token",
        "META_APP_SECRET": "ci-app-secret",
        # Without a recipient the escalation service records a delivery error instead of
        # sending, so the escalation tests would depend on a developer's own .env.
        "ESCALATION_HUMAN_WHATSAPP": "+10000000000",
        "MERCHANT_API_TOKEN": "test-merchant-token",
        "MERCHANT_KEY_ID": "dwarpal-merchant-test",
        "TRUST_REGISTRY_PATH": "./config/trust_registry.json",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    os.environ.update(FORCED)

    # Never point the suite at a development database.
    name = os.environ["DB_NAME"]
    if not name.endswith("_test"):
        os.environ["DB_NAME"] = f"{name}_test"


_bootstrap_environment()

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.settings import settings  # noqa: E402


def _ensure_database() -> None:
    """Create the test database if it does not exist, or explain why we cannot."""
    admin = create_engine(settings.maintenance_database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": settings.DB_NAME},
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{settings.DB_NAME}"'))
    except OperationalError as exc:
        raise pytest.UsageError(
            "Dwarpal's test suite requires PostgreSQL, because the budget and inventory guarantees "
            "are tested against real row-level locking.\n"
            f"  tried: {settings.DB_HOST}:{settings.DB_PORT} as {settings.DB_USER}\n"
            f"  error: {exc.orig}\n"
            "Start it from the repository root with: docker compose up -d\n"
            "If port 5432 is already taken on this machine, set DB_PORT in backend/.env and in a "
            "root .env so compose publishes the container on a free port."
        ) from exc
    finally:
        admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    _ensure_database()

    from app.db import base as db_base
    from app.db.bootstrap import create_schema

    db_base.engine.dispose()
    db_base.engine = create_engine(
        settings.database_url, pool_pre_ping=True, pool_size=20, max_overflow=20, future=True
    )
    db_base.SessionFactory.configure(bind=db_base.engine)
    create_schema(db_base.engine)
    yield
    db_base.engine.dispose()


TABLES_IN_TRUNCATION_ORDER = [
    "buyer_run_events",
    "buyer_runs",
    "notification_log",
    "agent_connections",
    "evidence_packets",
    "escalation_responses",
    "escalations",
    "refunds",
    "payment_exceptions",
    "payments",
    "verdicts",
    "spend_events",
    "budget_reservations",
    "inventory_holds",
    "credential_nonces",
    "idempotency_keys",
    "checkout_sessions",
    "open_mandates",
    "agent_identities",
    "disputes",
    "products",
    "policy_terms",
]


def _truncate(engine: object) -> None:
    from sqlalchemy import Engine

    assert isinstance(engine, Engine)
    with engine.begin() as connection:
        # TRUNCATE bypasses the row-level append-only trigger, which is exactly what a test
        # teardown needs and what application code can never do.
        connection.execute(
            text("TRUNCATE TABLE " + ", ".join(TABLES_IN_TRUNCATION_ORDER) + " RESTART IDENTITY")
        )


@pytest.fixture()
def db(database: None) -> Iterator[Session]:
    """A committing session. Concurrency tests need real commits, so nothing is rolled back."""
    from app.db import base as db_base

    _truncate(db_base.engine)
    session = db_base.SessionFactory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        _truncate(db_base.engine)


@pytest.fixture()
def seeded(db: Session) -> Session:
    from app.catalog.policy_terms import ensure_active_terms
    from app.db.bootstrap import seed_catalog

    seed_catalog(db)
    ensure_active_terms(db)
    db.commit()
    return db


@pytest.fixture(autouse=True)
def isolate_caches() -> Iterator[None]:
    """Reset cached singletons so runtime-registered trust keys do not leak between tests."""
    from app.trust import registry

    registry.reset_cache()
    yield
    registry.reset_cache()


@pytest.fixture()
def gateway() -> object:
    from app.payments.gateway import StubGateway

    return StubGateway()


@pytest.fixture()
def whatsapp() -> object:
    from app.escalation.whatsapp import RecordingTransport

    return RecordingTransport()

