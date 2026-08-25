"""Schema creation, append-only enforcement and catalog seeding."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.base import Base, engine
from app.db.models import Product
from app.settings import settings

# Append-only is a property of the store, not a promise the application makes about itself. A
# trigger means a retroactive edit fails even for someone with a psql prompt.
_APPEND_ONLY_TRIGGER = """
CREATE OR REPLACE FUNCTION dwarpal_evidence_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'evidence_packets is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evidence_append_only ON evidence_packets;
CREATE TRIGGER trg_evidence_append_only
    BEFORE UPDATE OR DELETE ON evidence_packets
    FOR EACH ROW EXECUTE FUNCTION dwarpal_evidence_append_only();
"""


# create_all adds tables but never columns, and this project has no migration tool. Columns added
# to an existing table therefore need saying once, here, in a form that is safe to run on every
# start. Postgres supports IF NOT EXISTS on ADD COLUMN, so this is idempotent and costs nothing
# after the first run.
_ADDED_COLUMNS = """
ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url TEXT NOT NULL DEFAULT '';
ALTER TABLE products ADD COLUMN IF NOT EXISTS image_alt VARCHAR(256) NOT NULL DEFAULT '';
"""


def create_schema(target: Engine | None = None) -> None:
    bind = target or engine
    Base.metadata.create_all(bind)
    with bind.begin() as connection:
        connection.execute(text(_ADDED_COLUMNS))
        connection.execute(text(_APPEND_ONLY_TRIGGER))


def drop_schema(target: Engine | None = None) -> None:
    bind = target or engine
    with bind.begin() as connection:
        connection.execute(
            text("DROP TRIGGER IF EXISTS trg_evidence_append_only ON evidence_packets")
        )
    Base.metadata.drop_all(bind)


def load_catalog_seed() -> list[dict[str, Any]]:
    path = settings.resolve(settings.CATALOG_SEED_PATH)
    if not path.exists():
        raise FileNotFoundError(f"catalog seed not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("catalog seed must be a JSON array of products")
    return data


def seed_catalog(session: Session, *, replace: bool = False) -> int:
    """Insert seed products that are not already present. Existing stock is left alone."""
    seeded = 0
    for entry in load_catalog_seed():
        existing = session.scalar(select(Product).where(Product.sku == entry["sku"]))
        if existing is not None:
            if not replace:
                continue
            for key, value in entry.items():
                setattr(existing, key, value)
            seeded += 1
            continue
        session.add(Product(**entry))
        seeded += 1
    session.flush()
    return seeded
