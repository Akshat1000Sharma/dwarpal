"""The Evidence Locker.

Every agent transaction produces a packet that is written once and never mutated. Packets are
hash chained, so each entry commits to its predecessor and any retroactive edit or deletion is
detectable. Append-only is additionally enforced by a database trigger, so the guarantee does not
depend on the application behaving.

Packets are written even when the transaction is refused, and especially when it is compensated.
The refusals are the more valuable evidence.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ap2.jose import canonical_json, sha256_b64url, sign_jws
from app.ap2.vocabulary import EVIDENCE_JWT_TYP
from app.db.base import utcnow
from app.db.models import EvidencePacket
from app.keys import merchant_key
from app.settings import settings

GENESIS_HASH = "GENESIS"


def compute_entry_hash(
    *, seq: int, correlation_id: str, prev_hash: str, body: dict[str, Any], created_at: str
) -> str:
    """The chain commitment. Any change to any field breaks every later link."""
    return sha256_b64url(
        canonical_json(
            {
                "seq": seq,
                "correlation_id": correlation_id,
                "prev_hash": prev_hash,
                "body": body,
                "created_at": created_at,
            }
        )
    )


def head(session: Session) -> EvidencePacket | None:
    return session.scalar(select(EvidencePacket).order_by(EvidencePacket.seq.desc()).limit(1))


def next_sequence(session: Session) -> int:
    return int(session.scalar(select(func.coalesce(func.max(EvidencePacket.seq), 0))) or 0) + 1


def append(session: Session, *, correlation_id: str, body: dict[str, Any]) -> EvidencePacket:
    """Write one packet, chained to the current head and signed by the merchant key."""
    previous = head(session)
    prev_hash = previous.entry_hash if previous is not None else GENESIS_HASH
    seq = (previous.seq + 1) if previous is not None else 1
    created_at = utcnow()
    created_iso = created_at.isoformat()

    entry_hash = compute_entry_hash(
        seq=seq,
        correlation_id=correlation_id,
        prev_hash=prev_hash,
        body=body,
        created_at=created_iso,
    )
    signature = sign_jws(
        {
            "iss": settings.MERCHANT_ID,
            "iat": int(created_at.timestamp()),
            "seq": seq,
            "correlation_id": correlation_id,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
        },
        merchant_key(),
        typ=EVIDENCE_JWT_TYP,
    )

    packet = EvidencePacket(
        seq=seq,
        correlation_id=correlation_id,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        signature=signature,
        body=body,
        created_at=created_at,
    )
    session.add(packet)
    session.flush()
    return packet


def for_correlation(session: Session, correlation_id: str) -> list[EvidencePacket]:
    return list(
        session.scalars(
            select(EvidencePacket)
            .where(EvidencePacket.correlation_id == correlation_id)
            .order_by(EvidencePacket.seq)
        ).all()
    )


def recent(session: Session, limit: int = 50, offset: int = 0) -> list[EvidencePacket]:
    return list(
        session.scalars(
            select(EvidencePacket).order_by(EvidencePacket.seq.desc()).limit(limit).offset(offset)
        ).all()
    )


def export_rows(session: Session) -> list[dict[str, Any]]:
    """The exact shape the standalone verifier reads."""
    packets = session.scalars(select(EvidencePacket).order_by(EvidencePacket.seq)).all()
    return [
        {
            "seq": p.seq,
            "packet_id": p.packet_id,
            "correlation_id": p.correlation_id,
            "prev_hash": p.prev_hash,
            "entry_hash": p.entry_hash,
            "signature": p.signature,
            "body": p.body,
            "created_at": p.created_at.isoformat(),
        }
        for p in packets
    ]


def verify_chain(session: Session) -> dict[str, Any]:
    """In-process chain check. The authoritative check is the standalone tool in tools/."""
    rows = export_rows(session)
    problems: list[dict[str, Any]] = []
    expected_prev = GENESIS_HASH
    expected_seq = 1
    for row in rows:
        if row["seq"] != expected_seq:
            problems.append(
                {"seq": row["seq"], "problem": "sequence_gap", "expected": expected_seq}
            )
        if row["prev_hash"] != expected_prev:
            problems.append({"seq": row["seq"], "problem": "broken_link"})
        recomputed = compute_entry_hash(
            seq=row["seq"],
            correlation_id=row["correlation_id"],
            prev_hash=row["prev_hash"],
            body=row["body"],
            created_at=row["created_at"],
        )
        if recomputed != row["entry_hash"]:
            problems.append({"seq": row["seq"], "problem": "body_altered"})
        expected_prev = row["entry_hash"]
        expected_seq = row["seq"] + 1
    return {"packets": len(rows), "valid": not problems, "problems": problems}
