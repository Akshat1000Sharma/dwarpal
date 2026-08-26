"""Merchant signing key management.

Keys are generated on first run into the configured directory, which is gitignored. In a deployed
environment that directory must be persistent storage, otherwise records signed before a restart
stop verifying.

Signing always uses the current key. Verification must also work for keys that have been rotated
out, because evidence packets are signed with whatever key was live when they were written and are
never rewritten. Retired public keys are therefore published alongside the current one: drop a
``<kid>.pem`` or ``<kid>.jwk`` into the ``retired`` subdirectory and the JWK Set will include it.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.ap2.jose import (
    KeyPair,
    generate_keypair,
    private_key_from_pem,
    private_key_to_pem,
    public_jwk,
    public_key_from_pem,
)
from app.logging import get_logger
from app.settings import settings

logger = get_logger(__name__)

_KEY_FILENAME = "merchant_signing_key.pem"
_RETIRED_DIRNAME = "retired"


def key_directory() -> Path:
    return settings.resolve(settings.MERCHANT_SIGNING_KEY_DIR)


def key_path() -> Path:
    return key_directory() / _KEY_FILENAME


@lru_cache(maxsize=1)
def merchant_key() -> KeyPair:
    """Load the merchant key, generating it on first run."""
    path = key_path()
    if path.exists():
        return KeyPair(
            kid=settings.MERCHANT_KEY_ID, private_key=private_key_from_pem(path.read_bytes())
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    keypair = generate_keypair(settings.MERCHANT_KEY_ID)
    # Created with the mode already set, not chmod'ed afterwards: writing first leaves the private
    # key readable by anybody for as long as the two calls take. Windows does not honour POSIX
    # modes here, and the directory is gitignored either way.
    mode = stat.S_IRUSR | stat.S_IWUSR
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private_key_to_pem(keypair.private_key))
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
    return keypair


def retired_directory() -> Path:
    return key_directory() / _RETIRED_DIRNAME


def _retired_jwks() -> list[dict[str, Any]]:
    """Public halves of keys no longer used for signing, still needed to check old records.

    The file stem is the key id, so a packet's ``kid`` finds its key. A malformed file is skipped
    rather than raised on: one unreadable retired key must not stop the current key being served.
    """
    directory = retired_directory()
    if not directory.is_dir():
        return []
    keys: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".pem", ".jwk", ".json"):
            continue
        try:
            if path.suffix == ".pem":
                loaded = public_key_from_pem(path.read_bytes())
                keys.append(public_jwk(loaded, path.stem))
            else:
                keys.append({**json.loads(path.read_text(encoding="utf-8")), "kid": path.stem})
        except Exception:
            logger.warning(
                "skipping an unreadable retired key", extra={"context": {"file": path.name}}
            )
    return keys


def merchant_jwks() -> dict[str, Any]:
    """The public JWK Set used to verify anything the merchant signed, past or present.

    Retired keys are included. Evidence packets are append-only and keep the signature made by the
    key that was live at the time, so publishing only the current key would make every record
    written before a rotation unverifiable.
    """
    key = merchant_key()
    published = [public_jwk(key.public_key, key.kid), *_retired_jwks()]
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for jwk in published:
        kid = str(jwk.get("kid", ""))
        if kid in seen:
            continue
        seen.add(kid)
        unique.append({**jwk, "use": "sig", "alg": "ES256"})
    return {"keys": unique}


def reset_cache() -> None:
    merchant_key.cache_clear()
