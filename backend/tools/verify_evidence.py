#!/usr/bin/env python3
"""Standalone Evidence Locker verifier.

This tool deliberately imports nothing from the Dwarpal application. It re-checks every hash link
and every signature from stored data alone, using only the standard library plus cryptography (and
psycopg2 when reading straight from the database). If it needed the running application to pass,
it would prove nothing.

Usage:

    python tools/verify_evidence.py --jsonl evidence.jsonl --jwks merchant_jwks.json
    python tools/verify_evidence.py --dsn postgresql://user:pass@host:5432/dwarpal --jwks jwks.json
    python tools/verify_evidence.py --jsonl evidence.jsonl --pem secrets/merchant_keys/key.pem

Exit status is 0 when the chain is intact and every signature verifies, and 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

GENESIS_HASH = "GENESIS"


# --- minimal JOSE, reimplemented here so the tool stands alone ---------------------------------


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def sha256_b64url(raw: bytes) -> str:
    return b64url_encode(hashlib.sha256(raw).digest())


def compute_entry_hash(row: dict[str, Any]) -> str:
    return sha256_b64url(
        canonical_json(
            {
                "seq": row["seq"],
                "correlation_id": row["correlation_id"],
                "prev_hash": row["prev_hash"],
                "body": row["body"],
                "created_at": row["created_at"],
            }
        )
    )


def load_public_keys(jwks_path: str | None, pem_path: str | None) -> list[Any]:
    """Every key a packet might have been signed with, not merely the current one.

    Packets are append-only and keep the signature made by the key that was live when they were
    written, so a chain that spans a key rotation needs all of them. Taking only the first key in
    the set silently fails every packet older than the most recent rotation.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if jwks_path:
        data = json.loads(Path(jwks_path).read_text(encoding="utf-8"))
        entries = data.get("keys", [data]) if isinstance(data, dict) else data
        loaded = []
        for jwk in entries:
            x = int.from_bytes(b64url_decode(jwk["x"]), "big")
            y = int.from_bytes(b64url_decode(jwk["y"]), "big")
            loaded.append(ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key())
        if not loaded:
            raise SystemExit("the JWK Set contains no keys")
        return loaded
    if pem_path:
        raw = Path(pem_path).read_bytes()
        if b"PRIVATE KEY" in raw:
            return [serialization.load_pem_private_key(raw, password=None).public_key()]
        return [serialization.load_pem_public_key(raw)]
    raise SystemExit("one of --jwks or --pem is required to verify signatures")


def verify_es256_any(token: str, public_keys: list[Any]) -> dict[str, Any]:
    """Accept the token if any published key verifies it. Re-raise the last failure otherwise."""
    last: Exception = ValueError("no verification keys were supplied")
    for key in public_keys:
        try:
            return verify_es256(token, key)
        except Exception as exc:
            last = exc
    raise last


def verify_es256(token: str, public_key: Any) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("compact JWS must have three segments")
    header = json.loads(b64url_decode(parts[0]))
    if header.get("alg") != "ES256":
        raise ValueError(f"unexpected alg {header.get('alg')!r}")
    raw_signature = b64url_decode(parts[2])
    if len(raw_signature) != 64:
        raise ValueError("ES256 signature must be 64 bytes")
    der = utils.encode_dss_signature(
        int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big")
    )
    public_key.verify(der, f"{parts[0]}.{parts[1]}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return json.loads(b64url_decode(parts[1]))


# --- sources -----------------------------------------------------------------------------------


def rows_from_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return sorted(rows, key=lambda r: r["seq"])


def rows_from_dsn(dsn: str) -> list[dict[str, Any]]:
    import psycopg2

    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT seq, packet_id, correlation_id, prev_hash, entry_hash, signature, body,"
                " created_at FROM evidence_packets ORDER BY seq"
            )
            return [
                {
                    "seq": r[0],
                    "packet_id": r[1],
                    "correlation_id": r[2],
                    "prev_hash": r[3],
                    "entry_hash": r[4],
                    "signature": r[5],
                    "body": r[6],
                    "created_at": r[7].isoformat(),
                }
                for r in cursor.fetchall()
            ]
    finally:
        connection.close()


# --- verification --------------------------------------------------------------------------------


def verify(rows: list[dict[str, Any]], public_keys: list[Any]) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    expected_prev = GENESIS_HASH
    expected_seq = 1
    signatures_checked = 0

    for row in rows:
        seq = row["seq"]
        if seq != expected_seq:
            problems.append(
                {"seq": seq, "problem": "sequence_gap", "expected_seq": expected_seq}
            )
        if row["prev_hash"] != expected_prev:
            problems.append(
                {
                    "seq": seq,
                    "problem": "broken_chain_link",
                    "expected_prev_hash": expected_prev,
                    "stored_prev_hash": row["prev_hash"],
                }
            )
        recomputed = compute_entry_hash(row)
        if recomputed != row["entry_hash"]:
            problems.append(
                {
                    "seq": seq,
                    "problem": "packet_body_altered",
                    "stored_entry_hash": row["entry_hash"],
                    "recomputed_entry_hash": recomputed,
                }
            )
        try:
            claims = verify_es256_any(row["signature"], public_keys)
            signatures_checked += 1
            if claims.get("entry_hash") != row["entry_hash"]:
                problems.append({"seq": seq, "problem": "signature_covers_a_different_entry"})
            if int(claims.get("seq", -1)) != seq:
                problems.append({"seq": seq, "problem": "signature_sequence_mismatch"})
        except Exception as exc:  # any failure here is the same finding
            problems.append(
                {
                    "seq": seq,
                    "problem": "signature_invalid",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        expected_prev = row["entry_hash"]
        expected_seq = seq + 1

    return {
        "packets": len(rows),
        "signatures_checked": signatures_checked,
        "valid": not problems,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Dwarpal evidence chain offline.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--jsonl", help="path to an exported evidence JSONL file")
    source.add_argument("--dsn", help="PostgreSQL connection string to read packets directly")
    parser.add_argument("--jwks", help="path to the merchant public JWK Set")
    parser.add_argument("--pem", help="path to the merchant key in PEM form")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--min-packets",
        type=int,
        default=1,
        help=(
            "fail if fewer than this many packets were read. An empty chain is vacuously valid, "
            "so without a floor a verifier pointed at the wrong store reports success while "
            "checking nothing. Pass 0 to allow an empty chain."
        ),
    )
    args = parser.parse_args(argv)

    rows = rows_from_jsonl(args.jsonl) if args.jsonl else rows_from_dsn(args.dsn)
    public_keys = load_public_keys(args.jwks, args.pem)
    report = verify(rows, public_keys)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"packets read        : {report['packets']}")
        print(f"signatures verified : {report['signatures_checked']}")
        print(f"chain valid         : {report['valid']}")
        for problem in report["problems"]:
            print(f"  seq {problem['seq']}: {problem['problem']}")
    if report["packets"] < args.min_packets:
        print(
            f"too few packets: read {report['packets']}, expected at least {args.min_packets}",
            file=sys.stderr,
        )
        return 1
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
