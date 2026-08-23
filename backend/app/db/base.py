"""Engine, session factory and declarative base.

Synchronous SQLAlchemy on psycopg2 is deliberate. The budget and inventory guarantees rest on
``SELECT ... FOR UPDATE`` held across a transaction and exercised by real concurrent threads;
sync sessions make that straightforward to write and honest to fuzz. FastAPI route functions are
declared with ``def`` so Starlette runs them in its threadpool and the concurrency is real.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, TypeDecorator, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.settings import settings


class Base(DeclarativeBase):
    pass


class UtcDateTime(TypeDecorator):
    """Timestamps are UTC everywhere. Naive values are rejected rather than guessed at."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected: Dwarpal stores UTC-aware timestamps only")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    future=True,
)

SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
