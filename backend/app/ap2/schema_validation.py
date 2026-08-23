"""Conformance checking against the vendored AP2 JSON Schemas.

Everything Dwarpal issues and everything it accepts is validated against the published schemas.
This is the machine-checkable half of the compliance claim: it demonstrates interoperation with
the reference implementation's own definitions rather than asserting conformance in a README.

Relative ``$ref`` values in the upstream schemas are resolved from the local vendored copies, so
validation never touches the network.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"

# Schema name to path, relative to SCHEMA_ROOT.
SCHEMAS: dict[str, str] = {
    "open_checkout_mandate": "ap2/open_checkout_mandate.json",
    "checkout_mandate": "ap2/checkout_mandate.json",
    "open_payment_mandate": "ap2/open_payment_mandate.json",
    "payment_mandate": "ap2/payment_mandate.json",
    "checkout_receipt": "ap2/checkout_receipt.json",
    "payment_receipt": "ap2/payment_receipt.json",
    "checkout": "ucp/types/checkout.json",
}


class SchemaConformanceError(ValueError):
    """Raised when a credential does not conform to its published AP2 schema."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        super().__init__(
            f"{schema_name} does not conform to the published AP2 schema: " + "; ".join(errors)
        )
        self.schema_name = schema_name
        self.errors = errors


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


CANONICAL_BASE = "https://ap2-protocol.org/schemas/"


def _candidate_uris(relative: str, declared_id: str) -> set[str]:
    """Every URI a sibling schema might use to reach this file.

    The upstream schemas are not internally consistent about identifiers: receipt_status.json
    declares itself as ".../receipt-status.json" but is referenced as "types/receipt_status.json".
    Registering each file under all of its plausible names lets validation succeed against the
    published schemas unmodified, which is the point of vendoring them verbatim.
    """
    parts = relative.split("/")
    suffixes = ["/".join(parts[depth:]) for depth in range(len(parts))]
    uris = {declared_id} if declared_id else set()
    for suffix in suffixes:
        uris.add(suffix)
        uris.add(f"./{suffix}")
        uris.add(CANONICAL_BASE + suffix)
    return uris - {""}


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources: list[tuple[str, Resource]] = []
    for file in sorted(SCHEMA_ROOT.rglob("*.json")):
        contents = _load(file)
        resource = Resource.from_contents(contents, default_specification=DRAFT202012)
        relative = file.relative_to(SCHEMA_ROOT).as_posix()
        for uri in _candidate_uris(relative, contents.get("$id", "")):
            resources.append((uri, resource))
    return Registry().with_resources(resources)


@lru_cache(maxsize=len(SCHEMAS))
def validator_for(schema_name: str) -> Draft202012Validator:
    try:
        relative = SCHEMAS[schema_name]
    except KeyError as exc:
        raise KeyError(f"unknown schema {schema_name!r}; known: {sorted(SCHEMAS)}") from exc
    schema = _load(SCHEMA_ROOT / relative)
    base = "/".join(relative.split("/")[:-1]) + "/"
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = _registry().with_resource(base, resource)
    return Draft202012Validator(schema, registry=registry)


def conformance_errors(schema_name: str, payload: Any) -> list[str]:
    validator = validator_for(schema_name)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]


def assert_conforms(schema_name: str, payload: Any) -> None:
    """Raise if the payload does not conform to the published schema."""
    errors = conformance_errors(schema_name, payload)
    if errors:
        raise SchemaConformanceError(schema_name, errors)


def conforms(schema_name: str, payload: Any) -> bool:
    return not conformance_errors(schema_name, payload)
