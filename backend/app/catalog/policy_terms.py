"""Merchant policy terms, signed and addressed by content hash.

An agent must acknowledge a specific policy hash inside its closed Checkout Mandate. If the
acknowledged hash is not the one that was live when the cart was quoted, the checkout does not
proceed. That acknowledgment is evidence that the buyer was told the terms, so it is collected and
stored, not treated as a formality.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ap2.jose import b64url_encode, sign_jws
from app.ap2.vocabulary import POLICY_TERMS_JWT_TYP
from app.db.base import utcnow
from app.db.models import PolicyTerms
from app.keys import merchant_key
from app.settings import settings


@dataclass(frozen=True)
class ActiveTerms:
    content_hash: str
    body: str
    signed_jwt: str
    effective_from: datetime

    def as_document(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "media_type": "text/markdown",
            "body": self.body,
            "signed_jwt": self.signed_jwt,
            "effective_from": self.effective_from.isoformat(),
        }


def content_hash(body: str) -> str:
    return b64url_encode(hashlib.sha256(body.encode("utf-8")).digest())


def read_terms_file() -> str:
    path = settings.resolve(settings.POLICY_TERMS_PATH)
    if not path.exists():
        raise FileNotFoundError(f"policy terms not found at {path}")
    return path.read_text(encoding="utf-8")


def _sign(body: str, digest: str, effective_from: datetime) -> str:
    return sign_jws(
        {
            "iss": settings.MERCHANT_ID,
            "iat": int(effective_from.timestamp()),
            "policy_hash": digest,
            "media_type": "text/markdown",
            "public_url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/policy/terms",
        },
        merchant_key(),
        typ=POLICY_TERMS_JWT_TYP,
    )


def ensure_active_terms(session: Session) -> ActiveTerms:
    """Publish the terms file as the live version, superseding an earlier one if it changed."""
    body = read_terms_file()
    digest = content_hash(body)

    existing = session.get(PolicyTerms, digest)
    if existing is not None and existing.effective_to is None:
        return ActiveTerms(digest, existing.body, existing.signed_jwt, existing.effective_from)

    now = utcnow()
    for superseded in session.scalars(
        select(PolicyTerms).where(PolicyTerms.effective_to.is_(None))
    ).all():
        if superseded.content_hash != digest:
            superseded.effective_to = now

    if existing is None:
        existing = PolicyTerms(
            content_hash=digest, body=body, signed_jwt=_sign(body, digest, now), effective_from=now
        )
        session.add(existing)
    else:
        existing.effective_to = None
    session.flush()
    return ActiveTerms(digest, existing.body, existing.signed_jwt, existing.effective_from)


def active_terms(session: Session) -> ActiveTerms:
    row = session.scalar(select(PolicyTerms).where(PolicyTerms.effective_to.is_(None)))
    if row is None:
        return ensure_active_terms(session)
    return ActiveTerms(row.content_hash, row.body, row.signed_jwt, row.effective_from)


def terms_by_hash(session: Session, digest: str) -> PolicyTerms | None:
    return session.get(PolicyTerms, digest)
