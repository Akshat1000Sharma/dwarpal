"""JOSE primitives for AP2.

ES256 over NIST P-256 throughout. The AP2 specification requires the Checkout JWT to be signed
with a non-deterministic signature scheme (ECDSA) rather than a deterministic one (Ed25519), so
the whole system uses a single ECDSA curve rather than mixing algorithms.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

ALG = "ES256"
CURVE = "P-256"
_COORD_BYTES = 32


class JoseError(ValueError):
    """Raised when a token or key is malformed, or a signature does not verify."""


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise JoseError(f"invalid base64url segment: {exc}") from exc


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding, so hashes over the same content always agree."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def sha256_b64url(raw: bytes) -> str:
    return b64url_encode(hashlib.sha256(raw).digest())


def hash_payload(payload: Any) -> str:
    return sha256_b64url(canonical_json(payload))


@dataclass(frozen=True)
class KeyPair:
    """An ECDSA P-256 key pair with a stable key identifier."""

    kid: str
    private_key: ec.EllipticCurvePrivateKey

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self.private_key.public_key()

    def public_jwk(self) -> dict[str, Any]:
        return public_jwk(self.public_key, self.kid)


def generate_keypair(kid: str) -> KeyPair:
    return KeyPair(kid=kid, private_key=ec.generate_private_key(ec.SECP256R1()))


def private_key_to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def private_key_from_pem(data: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise JoseError("merchant signing key must be an EC private key")
    return key


def public_jwk(key: ec.EllipticCurvePublicKey, kid: str | None = None) -> dict[str, Any]:
    numbers = key.public_numbers()
    jwk: dict[str, Any] = {
        "kty": "EC",
        "crv": CURVE,
        "x": b64url_encode(numbers.x.to_bytes(_COORD_BYTES, "big")),
        "y": b64url_encode(numbers.y.to_bytes(_COORD_BYTES, "big")),
    }
    if kid:
        jwk["kid"] = kid
    return jwk


def public_key_from_jwk(jwk: dict[str, Any]) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict):
        raise JoseError("jwk must be an object")
    if jwk.get("kty") != "EC" or jwk.get("crv") != CURVE:
        raise JoseError(f"unsupported key: expected EC {CURVE}")
    try:
        x = int.from_bytes(b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(b64url_decode(jwk["y"]), "big")
    except KeyError as exc:
        raise JoseError("jwk is missing coordinate") from exc
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 thumbprint, used as the canonical identity of an agent key."""
    required = {"crv": jwk.get("crv"), "kty": jwk.get("kty"), "x": jwk.get("x"), "y": jwk.get("y")}
    if not all(required.values()):
        raise JoseError("jwk is missing a member required for a thumbprint")
    return sha256_b64url(canonical_json(required))


def _raw_signature(der: bytes) -> bytes:
    r, s = asym_utils.decode_dss_signature(der)
    return r.to_bytes(_COORD_BYTES, "big") + s.to_bytes(_COORD_BYTES, "big")


def _der_signature(raw: bytes) -> bytes:
    if len(raw) != _COORD_BYTES * 2:
        raise JoseError("ES256 signature must be 64 bytes")
    r = int.from_bytes(raw[:_COORD_BYTES], "big")
    s = int.from_bytes(raw[_COORD_BYTES:], "big")
    return asym_utils.encode_dss_signature(r, s)


def sign_jws(
    payload: dict[str, Any],
    key: KeyPair,
    *,
    typ: str | None = None,
    extra_header: dict[str, Any] | None = None,
) -> str:
    header: dict[str, Any] = {"alg": ALG, "kid": key.kid}
    if typ:
        header["typ"] = typ
    if extra_header:
        header.update(extra_header)
    encoded_header = b64url_encode(canonical_json(header))
    encoded_payload = b64url_encode(canonical_json(payload))
    signing_input = f"{encoded_header}.{encoded_payload}"
    der = key.private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return f"{signing_input}.{b64url_encode(_raw_signature(der))}"


def decode_jws_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read header and payload without checking the signature. Never trust the result."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JoseError("compact JWS must have three segments")
    try:
        header = json.loads(b64url_decode(parts[0]))
        payload = json.loads(b64url_decode(parts[1]))
    except json.JSONDecodeError as exc:
        raise JoseError(f"JWS segment is not JSON: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JoseError("JWS header and payload must both be objects")
    return header, payload


def verify_jws(token: str, key: ec.EllipticCurvePublicKey) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise JoseError("compact JWS must have three segments")
    header, payload = decode_jws_unverified(token)
    if header.get("alg") != ALG:
        raise JoseError(f"unsupported alg {header.get('alg')!r}, expected {ALG}")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        key.verify(
            _der_signature(b64url_decode(parts[2])), signing_input, ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature as exc:
        raise JoseError("signature does not verify") from exc
    return payload
