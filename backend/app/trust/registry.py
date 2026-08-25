"""Trust registry: issuing authorities, their tiers, and what each tier may do.

Loaded and validated at startup. Adding an authority is a configuration change, never a code
change. A malformed registry stops the process rather than degrading silently at request time.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.settings import settings

UNVERIFIED_TIER = "unverified"

# Publishing is a read-modify-write of a shared file. Several agents minting identities at the
# same time would otherwise each read the same set of keys and each write back their own, so all
# but the last one would vanish and the merchant would refuse their credentials as unsigned.
_PUBLISH_LOCK = threading.Lock()


class TrustRegistryError(RuntimeError):
    """Raised when the trust registry is missing or malformed."""


@dataclass(frozen=True)
class Tier:
    name: str
    rank: int
    description: str
    max_transaction_minor: int
    allow_restricted_categories: bool
    allow_age_restricted: bool
    requires_key_binding: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rank": self.rank,
            "description": self.description,
            "max_transaction_minor": self.max_transaction_minor,
            "allow_restricted_categories": self.allow_restricted_categories,
            "allow_age_restricted": self.allow_age_restricted,
            "requires_key_binding": self.requires_key_binding,
        }


@dataclass(frozen=True)
class Authority:
    issuer_id: str
    name: str
    tier: str
    jwks: list[dict[str, Any]] = field(default_factory=list)
    jwks_path: str | None = None

    def published_keys(self) -> list[dict[str, Any]]:
        """Inline keys plus anything the authority publishes in its JWKS file.

        The file is read on each call rather than cached, so an authority that rotates or adds a
        key does not need the merchant restarted. Adding the authority itself is still a
        configuration change.

        The read is retried because an authority publishing a key replaces this file, and Windows
        refuses a reader for the moment the swap takes. Answering that with a 500, or silently
        with a short key set, would both surface as a genuine credential being refused.
        """
        keys = list(self.jwks)
        if not self.jwks_path:
            return keys
        path = settings.resolve(self.jwks_path)
        document = _read_json(path)
        if document is None:
            return keys
        published = document.get("keys", document) if isinstance(document, dict) else document
        for key in published if isinstance(published, list) else []:
            if isinstance(key, dict) and key not in keys:
                keys.append(key)
        return keys


def _read_json(path: Path, attempts: int = 20) -> Any:
    """Read a JSON file that another process may be replacing right now.

    Returns None when the file is absent or unreadable after every attempt, which the caller
    treats as "this authority has published nothing", never as "this signature is forged".
    """
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None
        except OSError:
            if attempt == attempts - 1:
                return None
            time.sleep(0.01 * (attempt + 1))
    return None


@dataclass(frozen=True)
class TrustRegistry:
    tiers: dict[str, Tier]
    authorities: dict[str, Authority]

    def tier_for_issuer(self, issuer_id: str | None) -> Tier | None:
        if issuer_id is None:
            return None
        authority = self.authorities.get(issuer_id)
        if authority is None:
            return None
        return self.tiers[authority.tier]

    def authority(self, issuer_id: str | None) -> Authority | None:
        return self.authorities.get(issuer_id) if issuer_id else None

    def unverified(self) -> Tier:
        return self.tiers[UNVERIFIED_TIER]

    def keys_for(self, issuer_id: str) -> list[dict[str, Any]]:
        authority = self.authorities.get(issuer_id)
        return authority.published_keys() if authority else []

    def trust_anchors(self) -> list[dict[str, Any]]:
        """What the discovery document advertises to an arriving agent."""
        return [
            {"issuer_id": a.issuer_id, "name": a.name, "tier": a.tier}
            for a in sorted(self.authorities.values(), key=lambda a: a.issuer_id)
        ]


_REQUIRED_TIER_FIELDS = (
    "rank",
    "max_transaction_minor",
    "allow_restricted_categories",
    "allow_age_restricted",
    "requires_key_binding",
)


def parse_registry(raw: dict[str, Any]) -> TrustRegistry:
    if not isinstance(raw, dict):
        raise TrustRegistryError("trust registry must be a JSON object")

    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, dict) or not raw_tiers:
        raise TrustRegistryError("trust registry must define at least one tier")

    tiers: dict[str, Tier] = {}
    for name, body in raw_tiers.items():
        if not isinstance(body, dict):
            raise TrustRegistryError(f"tier {name!r} must be an object")
        missing = [f for f in _REQUIRED_TIER_FIELDS if f not in body]
        if missing:
            raise TrustRegistryError(f"tier {name!r} is missing {missing}")
        tiers[name] = Tier(
            name=name,
            rank=int(body["rank"]),
            description=str(body.get("description", "")),
            max_transaction_minor=int(body["max_transaction_minor"]),
            allow_restricted_categories=bool(body["allow_restricted_categories"]),
            allow_age_restricted=bool(body["allow_age_restricted"]),
            requires_key_binding=bool(body["requires_key_binding"]),
        )

    if UNVERIFIED_TIER not in tiers:
        raise TrustRegistryError(
            f"trust registry must define the {UNVERIFIED_TIER!r} tier: an agent that presents no "
            "acceptable credential still gets a smaller door, not a closed one"
        )

    authorities: dict[str, Authority] = {}
    for entry in raw.get("authorities", []) or []:
        if not isinstance(entry, dict):
            raise TrustRegistryError("each authority must be an object")
        issuer_id = entry.get("issuer_id")
        tier_name = entry.get("tier")
        if not issuer_id:
            raise TrustRegistryError("an authority is missing issuer_id")
        if tier_name not in tiers:
            raise TrustRegistryError(f"authority {issuer_id!r} names unknown tier {tier_name!r}")
        if issuer_id in authorities:
            raise TrustRegistryError(f"authority {issuer_id!r} is declared twice")
        authorities[issuer_id] = Authority(
            issuer_id=str(issuer_id),
            name=str(entry.get("name", issuer_id)),
            tier=str(tier_name),
            jwks=list(entry.get("jwks", []) or []),
            jwks_path=entry.get("jwks_path"),
        )

    return TrustRegistry(tiers=tiers, authorities=authorities)


@lru_cache(maxsize=1)
def get_registry() -> TrustRegistry:
    path = settings.resolve(settings.TRUST_REGISTRY_PATH)
    if not path.exists():
        raise TrustRegistryError(f"trust registry not found at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustRegistryError(f"trust registry at {path} is not valid JSON: {exc}") from exc
    return parse_registry(raw)


def reset_cache() -> None:
    get_registry.cache_clear()


def register_runtime_key(issuer_id: str, jwk: dict[str, Any]) -> None:
    """Add a public key to an authority already declared in the registry, in this process only.

    Used by the mocked Credential Provider, which mints its key material at run time. It cannot
    introduce a new authority, so the set of trusted issuers stays a configuration decision.
    """
    registry = get_registry()
    authority = registry.authorities.get(issuer_id)
    if authority is None:
        raise TrustRegistryError(
            f"{issuer_id!r} is not declared in the trust registry; add it to the configuration file"
        )
    if jwk not in authority.jwks:
        authority.jwks.append(jwk)


def publish_key(issuer_id: str, jwk: dict[str, Any]) -> Path:
    """Write a key into the authority's published JWKS file, so another process can see it.

    The interop driver runs as a separate process from the merchant, so an in-memory registration
    would be invisible to it. Publishing to the file the authority already declares is what a real
    issuing authority does.
    """
    registry = get_registry()
    authority = registry.authorities.get(issuer_id)
    if authority is None or not authority.jwks_path:
        raise TrustRegistryError(f"{issuer_id!r} declares no jwks_path to publish into")
    path = settings.resolve(authority.jwks_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PUBLISH_LOCK:
        existing: list[dict[str, Any]] = []
        if path.exists():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                existing = (
                    document.get("keys", []) if isinstance(document, dict) else list(document)
                )
            except json.JSONDecodeError:
                existing = []
        if jwk not in existing:
            existing.append(jwk)
        _write_atomically(path, {"keys": existing})
    return path


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    """Replace the file in one step, so a reader never sees a half-written JWKS.

    The merchant reads this file on every verification, and a truncated read would present as a
    forged signature rather than as the file-system race it actually is.
    """
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _replace_with_retry(temporary: str, path: Path, attempts: int = 20) -> None:
    """os.replace, retried, because Windows refuses it while another process has the file open.

    The merchant reads this file on every verification, so a reader holding it for the microsecond
    the swap needs is normal rather than exceptional. POSIX replaces regardless; Windows raises
    PermissionError, and giving up would drop a key an agent is about to present.
    """
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))
