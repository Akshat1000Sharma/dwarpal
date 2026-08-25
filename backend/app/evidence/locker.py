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

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ap2.jose import canonical_json, sha256_b64url, sign_jws
from app.ap2.vocabulary import EVIDENCE_JWT_TYP
from app.db.base import utcnow
from app.db.models import EvidencePacket
from app.keys import merchant_key
from app.settings import settings

GENESIS_HASH = "GENESIS"

# A hash chain is serial by definition: every entry commits to the one before it, so two writers
# choosing the same predecessor is the chain forking, not a contention problem to tune away.
#
# So appends are serialised by an advisory lock rather than raced and retried. Retrying was tried
# first and is not good enough: under contention writers keep colliding, and an evidence packet
# that was dropped after too many attempts is the one outcome this module exists to prevent.
#
# The lock is transaction-scoped, so it is held until the caller commits. That is only safe
# because nothing slow happens after an append: the packet is the last thing written before a
# checkout returns, and the WhatsApp receipt that used to sit here was moved off the request in
# app/notify/service.py precisely so this lock is never held across a network call.
_CHAIN_LOCK_KEY = 8_474_101_982_735_461

# The lock makes a collision impossible in one process against one database. The retry stays as a
# belt to its braces, because losing a packet is worse than a slow append.
_APPEND_ATTEMPTS = 8


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
    """Write one packet, chained to the current head and signed by the merchant key.

    Two concurrent checkouts read the same head, compute the same sequence number and the same
    prev_hash, and one of them loses on the primary key. Losing is correct: it is what stops the
    chain forking. What must not happen is the loser answering the agent with a 500 and no packet
    filed, so the attempt is repeated against a freshly read head until it wins.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})
    last: IntegrityError | None = None
    for _attempt in range(_APPEND_ATTEMPTS):
        try:
            with session.begin_nested():
                return _write(session, correlation_id=correlation_id, body=body)
        except IntegrityError as exc:
            # Another writer took this sequence number. The savepoint is gone and the outer
            # transaction is intact, so the next read sees their committed row.
            last = exc
    raise RuntimeError(
        f"could not append an evidence packet after {_APPEND_ATTEMPTS} attempts"
    ) from last


def _write(session: Session, *, correlation_id: str, body: dict[str, Any]) -> EvidencePacket:
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


def export_rows(session: Session, *, seqs: set[int] | None = None) -> list[dict[str, Any]]:
    """The exact shape the standalone verifier reads."""
    statement = select(EvidencePacket).order_by(EvidencePacket.seq)
    if seqs is not None:
        statement = statement.where(EvidencePacket.seq.in_(sorted(seqs)))
    packets = session.scalars(statement).all()
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


def verify_chain(session: Session, *, seqs: set[int] | None = None) -> dict[str, Any]:
    """In-process chain check. The authoritative check is the standalone tool in tools/.

    ``seqs`` restricts the rehashing to the packets a caller is actually rendering, plus each one's
    predecessor so its link can still be checked. Reading and rehashing every packet ever written
    is right for a command line sweep and far too much work for a page view.
    """
    total = int(session.scalar(select(func.count(EvidencePacket.seq))) or 0)
    scoped = seqs is not None
    if scoped:
        assert seqs is not None
        rows = export_rows(session, seqs=set(seqs) | {s - 1 for s in seqs if s > 1})
    else:
        rows = export_rows(session)

    problems: list[dict[str, Any]] = []
    by_seq = {row["seq"]: row for row in rows}
    expected_seq = 1
    for row in rows:
        seq = row["seq"]
        if not scoped:
            if seq != expected_seq:
                problems.append({"seq": seq, "problem": "sequence_gap", "expected": expected_seq})
            expected_seq = seq + 1
        elif seq not in seqs:
            continue
        previous = by_seq.get(seq - 1)
        expected_prev = GENESIS_HASH if seq == 1 else (previous or {}).get("entry_hash")
        if expected_prev is not None and row["prev_hash"] != expected_prev:
            problems.append({"seq": seq, "problem": "broken_link"})
        recomputed = compute_entry_hash(
            seq=seq,
            correlation_id=row["correlation_id"],
            prev_hash=row["prev_hash"],
            body=row["body"],
            created_at=row["created_at"],
        )
        if recomputed != row["entry_hash"]:
            problems.append({"seq": seq, "problem": "body_altered"})
    return {"packets": total, "valid": not problems, "problems": problems}
