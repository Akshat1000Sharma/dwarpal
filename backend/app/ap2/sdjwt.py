"""SD-JWT with key binding, per RFC 9901 and the AP2 binding rules.

Implemented here rather than taken from the reference SDK because the adversarial corpus has to
forge signatures, strip disclosures, swap holder keys and re-bind tokens at the byte level. A
high-level SDK deliberately makes those things impossible, which would leave the attack families
the adversarial corpus depends on untestable.

Wire format:

    <issuer-jwt>~<disclosure>~...~            no key binding
    <issuer-jwt>~<disclosure>~...~<kb-jwt>    with key binding

A disclosure is base64url(JSON) of ``[salt, name, value]`` for an object member, or
``[salt, value]`` for an array element. The issuer JWT carries ``_sd`` digest arrays for hidden
object members and ``{"...": digest}`` placeholders for hidden array elements.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from app.ap2.jose import (
    ALG,
    JoseError,
    KeyPair,
    b64url_decode,
    b64url_encode,
    canonical_json,
    decode_jws_unverified,
    public_key_from_jwk,
    sign_jws,
    verify_jws,
)

SD_ALG = "sha-256"
KB_TYP = "kb+jwt"
ARRAY_PLACEHOLDER = "..."

_HASHES = {"sha-256": hashlib.sha256, "sha-384": hashlib.sha384, "sha-512": hashlib.sha512}


class SdJwtError(JoseError):
    """Raised when an SD-JWT is malformed, inconsistent, or fails verification."""


class KeyBindingError(SdJwtError):
    """Raised when the presenter does not control the key the credential was issued to."""


class MissingKeyBindingError(SdJwtError):
    """Raised when key binding was required and no KB-JWT was presented."""


@dataclass(frozen=True)
class SD:
    """Marks a value as selectively disclosable when issuing."""

    value: Any


def _digest(disclosure: str, sd_alg: str = SD_ALG) -> str:
    try:
        hasher = _HASHES[sd_alg]
    except KeyError as exc:
        raise SdJwtError(f"unsupported _sd_alg {sd_alg!r}") from exc
    return b64url_encode(hasher(disclosure.encode("ascii")).digest())


def _encode_disclosure(parts: list[Any]) -> str:
    return b64url_encode(canonical_json(parts))


def _new_salt() -> str:
    return b64url_encode(secrets.token_bytes(16))


def _blind(node: Any, disclosures: list[str]) -> Any:
    """Recursively replace SD-marked values with digests, collecting disclosures."""
    if isinstance(node, dict):
        plain: dict[str, Any] = {}
        hidden: list[str] = []
        for key, value in node.items():
            if isinstance(value, SD):
                blinded = _blind(value.value, disclosures)
                disclosure = _encode_disclosure([_new_salt(), key, blinded])
                disclosures.append(disclosure)
                hidden.append(_digest(disclosure))
            else:
                plain[key] = _blind(value, disclosures)
        if hidden:
            existing = plain.get("_sd", [])
            plain["_sd"] = sorted([*existing, *hidden])
        return plain
    if isinstance(node, list):
        out: list[Any] = []
        for item in node:
            if isinstance(item, SD):
                disclosure = _encode_disclosure([_new_salt(), _blind(item.value, disclosures)])
                disclosures.append(disclosure)
                out.append({ARRAY_PLACEHOLDER: _digest(disclosure)})
            else:
                out.append(_blind(item, disclosures))
        return out
    if isinstance(node, SD):
        raise SdJwtError("SD() may only wrap an object member or an array element")
    return node


def issue(
    claims: dict[str, Any],
    issuer_key: KeyPair,
    *,
    holder_jwk: dict[str, Any] | None = None,
    typ: str = "dc+sd-jwt",
) -> str:
    """Sign an SD-JWT. Values wrapped in ``SD`` become selectively disclosable."""
    disclosures: list[str] = []
    payload = _blind(dict(claims), disclosures)
    payload["_sd_alg"] = SD_ALG
    if holder_jwk is not None:
        payload["cnf"] = {"jwk": holder_jwk}
    issuer_jwt = sign_jws(payload, issuer_key, typ=typ)
    return issuer_jwt + "~" + "".join(d + "~" for d in disclosures)


@dataclass(frozen=True)
class ParsedSdJwt:
    issuer_jwt: str
    disclosures: tuple[str, ...]
    kb_jwt: str | None
    header: dict[str, Any]
    payload: dict[str, Any]

    @property
    def sd_alg(self) -> str:
        alg = self.payload.get("_sd_alg", SD_ALG)
        return alg if isinstance(alg, str) else SD_ALG

    @property
    def presentation(self) -> str:
        """The issuer JWT and disclosures, excluding any trailing KB-JWT."""
        return self.issuer_jwt + "~" + "".join(d + "~" for d in self.disclosures)

    @property
    def sd_hash(self) -> str:
        return _digest(self.presentation, self.sd_alg)

    def with_kb(self, kb_jwt: str) -> str:
        return self.presentation + kb_jwt


def parse(token: str) -> ParsedSdJwt:
    if not token or token.startswith("~"):
        raise SdJwtError("malformed SD-JWT: empty issuer JWT")
    if "~" not in token:
        raise SdJwtError("malformed SD-JWT: missing disclosure separator")
    segments = token.split("~")
    issuer_jwt = segments[0]
    if token.endswith("~"):
        disclosures = list(segments[1:-1])
        kb_jwt = None
    else:
        kb_jwt = segments[-1]
        disclosures = list(segments[1:-1])
        if len(kb_jwt.split(".")) != 3:
            raise SdJwtError("malformed KB-JWT: expected header.payload.signature")
    if any(not d for d in disclosures):
        raise SdJwtError("malformed SD-JWT: empty disclosure segment")
    header, payload = decode_jws_unverified(issuer_jwt)
    return ParsedSdJwt(issuer_jwt, tuple(disclosures), kb_jwt, header, payload)


def _decode_disclosure(disclosure: str) -> list[Any]:
    try:
        parsed = json.loads(b64url_decode(disclosure))
    except json.JSONDecodeError as exc:
        raise SdJwtError(f"disclosure is not JSON: {exc}") from exc
    if not isinstance(parsed, list) or len(parsed) not in (2, 3):
        raise SdJwtError("disclosure must be a 2- or 3-element array")
    return parsed


def _resolve(node: Any, by_digest: dict[str, list[Any]], used: set[str], sd_alg: str) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("_sd", "_sd_alg"):
                continue
            out[key] = _resolve(value, by_digest, used, sd_alg)
        for digest in node.get("_sd", []) or []:
            entry = by_digest.get(digest)
            if entry is None:
                continue  # withheld by the holder, which is the point of selective disclosure
            if len(entry) != 3:
                raise SdJwtError("object disclosure must be [salt, name, value]")
            _salt, name, value = entry
            if name in out:
                raise SdJwtError(f"disclosure {name!r} collides with a plaintext claim")
            used.add(digest)
            out[name] = _resolve(value, by_digest, used, sd_alg)
        return out
    if isinstance(node, list):
        out_list: list[Any] = []
        for item in node:
            if isinstance(item, dict) and set(item.keys()) == {ARRAY_PLACEHOLDER}:
                entry = by_digest.get(item[ARRAY_PLACEHOLDER])
                if entry is None:
                    continue
                if len(entry) != 2:
                    raise SdJwtError("array disclosure must be [salt, value]")
                used.add(item[ARRAY_PLACEHOLDER])
                out_list.append(_resolve(entry[1], by_digest, used, sd_alg))
            else:
                out_list.append(_resolve(item, by_digest, used, sd_alg))
        return out_list
    return node


@dataclass
class VerifiedSdJwt:
    claims: dict[str, Any]
    parsed: ParsedSdJwt
    key_bound: bool = False
    kb_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def cnf_jwk(self) -> dict[str, Any] | None:
        cnf = self.claims.get("cnf")
        if isinstance(cnf, dict) and isinstance(cnf.get("jwk"), dict):
            return cnf["jwk"]
        return None


def verify(
    token: str,
    issuer_jwk: dict[str, Any],
    *,
    expected_aud: str | None = None,
    expected_nonce: str | None = None,
    require_key_binding: bool = False,
) -> VerifiedSdJwt:
    """Verify the issuer signature, resolve disclosures, and check key binding when required."""
    parsed = parse(token)
    if parsed.header.get("alg") != ALG:
        raise SdJwtError(f"unsupported alg {parsed.header.get('alg')!r}")

    try:
        verify_jws(parsed.issuer_jwt, public_key_from_jwk(issuer_jwk))
    except SdJwtError:
        raise
    except JoseError as exc:
        raise SdJwtError(f"issuer signature invalid: {exc}") from exc

    sd_alg = parsed.sd_alg
    by_digest: dict[str, list[Any]] = {}
    for disclosure in parsed.disclosures:
        digest = _digest(disclosure, sd_alg)
        if digest in by_digest:
            raise SdJwtError("duplicate disclosure presented")
        by_digest[digest] = _decode_disclosure(disclosure)

    used: set[str] = set()
    claims = _resolve(parsed.payload, by_digest, used, sd_alg)
    unused = set(by_digest) - used
    if unused:
        # RFC 9901: a disclosure that matches no digest means the token was assembled from parts.
        raise SdJwtError(f"{len(unused)} disclosure(s) do not match any digest in the issuer JWT")

    result = VerifiedSdJwt(claims=claims, parsed=parsed)

    if parsed.kb_jwt is None:
        if require_key_binding:
            raise MissingKeyBindingError("key binding required but no KB-JWT was presented")
        return result

    cnf = result.cnf_jwk
    if cnf is None:
        raise KeyBindingError("KB-JWT presented but the credential carries no cnf.jwk")
    kb_header, _ = decode_jws_unverified(parsed.kb_jwt)
    if kb_header.get("typ") != KB_TYP:
        raise KeyBindingError(f"KB-JWT typ must be {KB_TYP!r}")
    try:
        kb_claims = verify_jws(parsed.kb_jwt, public_key_from_jwk(cnf))
    except JoseError as exc:
        # The presenter does not hold the key the credential was issued to. This is the
        # confused-deputy case and must be distinguishable from an issuer signature failure.
        raise KeyBindingError(f"key binding invalid: {exc}") from exc

    if kb_claims.get("sd_hash") != parsed.sd_hash:
        raise KeyBindingError("KB-JWT sd_hash does not match the presented disclosures")
    if "iat" not in kb_claims:
        raise KeyBindingError("KB-JWT is missing iat")
    if expected_aud is not None and kb_claims.get("aud") != expected_aud:
        raise KeyBindingError("KB-JWT aud mismatch")
    if expected_nonce is not None and kb_claims.get("nonce") != expected_nonce:
        raise KeyBindingError("KB-JWT nonce mismatch")

    result.key_bound = True
    result.kb_claims = kb_claims
    return result


def attach_key_binding(
    token: str,
    holder_key: KeyPair,
    *,
    audience: str,
    nonce: str,
    issued_at: int,
) -> str:
    """Produce the holder's proof of possession over exactly this presentation."""
    parsed = parse(token)
    if parsed.kb_jwt is not None:
        raise SdJwtError("token already carries a KB-JWT")
    kb = sign_jws(
        {"iat": issued_at, "aud": audience, "nonce": nonce, "sd_hash": parsed.sd_hash},
        holder_key,
        typ=KB_TYP,
    )
    return parsed.with_kb(kb)
