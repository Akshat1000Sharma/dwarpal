"""The seed data is what the whole demonstration is built on, so it has to be complete.

An item without a picture renders as a placeholder rather than a photograph. The placeholder is
deliberate and looks it, but a catalog where half the cards are placeholders looks unfinished, and
nothing else in the system would complain about it. This is the thing that complains.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import service as catalog
from app.db.bootstrap import load_catalog_seed
from app.db.models import Product

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PUBLIC = BACKEND_ROOT.parent / "frontend" / "public"
CREDITS = BACKEND_ROOT / "config" / "catalog_image_credits.json"


@pytest.fixture(scope="module")
def seed() -> list[dict[str, object]]:
    return load_catalog_seed()


def test_every_item_has_an_image(seed: list[dict[str, object]]) -> None:
    without = [item["sku"] for item in seed if not str(item.get("image_url", "")).strip()]
    assert not without, f"these items would render as placeholders: {without}"


def test_every_item_has_alt_text(seed: list[dict[str, object]]) -> None:
    """A picture with no alt text is invisible to anyone using a screen reader."""
    without = [item["sku"] for item in seed if not str(item.get("image_alt", "")).strip()]
    assert not without, f"these images have no alt text: {without}"


def test_every_local_image_actually_exists(seed: list[dict[str, object]]) -> None:
    """The seed points at a path; the file has to be there, or the card shows a placeholder.

    Images are vendored into the frontend's public folder rather than hotlinked. Hotlinking
    Wikimedia was measured at 64 of 122 optimizer fetches succeeding, the rest rate limited, which
    is fine for a wiki and useless for a shop.
    """
    missing: list[str] = []
    for item in seed:
        url = str(item.get("image_url", ""))
        if not url.startswith("/"):
            continue
        if not (PUBLIC / url.lstrip("/")).is_file():
            missing.append(f"{item['sku']} -> {url}")
    assert not missing, f"seed points at files that are not there: {missing}"


def test_no_image_is_an_empty_file(seed: list[dict[str, object]]) -> None:
    small: list[str] = []
    for item in seed:
        url = str(item.get("image_url", ""))
        if not url.startswith("/"):
            continue
        path = PUBLIC / url.lstrip("/")
        if path.is_file() and path.stat().st_size < 4096:
            small.append(f"{item['sku']} ({path.stat().st_size} bytes)")
    assert not small, f"these look like failed downloads: {small}"


def test_every_image_has_a_recorded_source_and_licence(seed: list[dict[str, object]]) -> None:
    """Somebody else's photograph is being served, so where it came from is part of the record."""
    credits = json.loads(CREDITS.read_text(encoding="utf-8"))
    for item in seed:
        sku = str(item["sku"])
        assert sku in credits, f"{sku} has an image with no recorded source"
        entry = credits[sku]
        assert entry.get("url"), f"{sku} has no upstream url recorded"
        assert entry.get("licence"), f"{sku} has no licence recorded"
        assert entry.get("page", "").startswith("https://"), f"{sku} has no source page"
        assert entry.get("changes"), f"{sku} does not say how the vendored copy was modified"


def test_every_attributed_image_names_its_creator(seed: list[dict[str, object]]) -> None:
    """CC BY and CC BY-SA oblige us to credit the photographer by name, and CC0 does not.

    Serving somebody's photograph without their name is the one licence term this repository could
    breach by omission, so it fails the suite rather than shipping.
    """
    credits = json.loads(CREDITS.read_text(encoding="utf-8"))
    missing = [
        sku
        for sku, entry in credits.items()
        if not str(entry.get("licence", "")).upper().startswith("CC0")
        and not str(entry.get("author", "")).strip()
    ]
    assert not missing, f"these images require attribution and name no author: {missing}"


def test_the_catalog_document_carries_the_image(seeded: Session) -> None:
    """What the API serves is what the console renders, so the image has to survive the mapping."""
    entries = catalog.browse(seeded, limit=100)
    assert entries, "the catalog seeded nothing"

    for entry in entries:
        document = entry.as_document()
        image = document.get("image")
        assert image is not None, f"{document['sku']} lost its image in as_document"
        assert image["url"], f"{document['sku']} has an empty image url"
        assert image["alt"], f"{document['sku']} has empty alt text"


def test_an_item_without_an_image_serves_null_rather_than_an_empty_string(seeded: Session) -> None:
    """The console decides between a photograph and a placeholder on this being null.

    An empty string would be truthy enough in some hands to reach an img tag, and that is exactly
    the empty box this whole change exists to remove.
    """
    product = seeded.scalar(select(Product).where(Product.sku == "DWP-TEA-001"))
    assert product is not None
    product.image_url = ""
    seeded.flush()

    entry = catalog.by_sku(seeded, "DWP-TEA-001")
    assert entry is not None
    assert entry.as_document()["image"] is None


def test_the_evidence_snapshot_records_what_was_shown(seeded: Session) -> None:
    """A catalog snapshot is meant to reconstruct what the buyer saw, and that includes the image."""
    entry = catalog.by_sku(seeded, "DWP-MNG-004")
    assert entry is not None
    snapshot = entry.snapshot()
    assert snapshot["image_url"], "the snapshot dropped the image the buyer was shown"
