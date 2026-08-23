"""Merchant signing key management.

Keys are generated on first run into the configured directory, which is gitignored. In a deployed
environment that directory must be persistent storage, otherwise records signed before a restart
stop verifying.
"""

from __future__ import annotations

import contextlib
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
)
from app.settings import settings

_KEY_FILENAME = "merchant_signing_key.pem"


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
    path.write_bytes(private_key_to_pem(keypair.private_key))
    # Windows does not honour POSIX modes here; the directory is gitignored either way.
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return keypair


def merchant_jwks() -> dict[str, Any]:
    """The public JWK Set an agent uses to verify anything the merchant signed."""
    key = merchant_key()
    jwk = public_jwk(key.public_key, key.kid)
    return {"keys": [{**jwk, "use": "sig", "alg": "ES256"}]}


def reset_cache() -> None:
    merchant_key.cache_clear()
