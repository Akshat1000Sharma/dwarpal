"""Persistent model.

Money is always an integer in the currency's minor unit. Timestamps are UTC-aware. Every record
that belongs to a transaction carries the correlation id that ties the credential chain, the
verdicts, the payment and the evidence packet together.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UtcDateTime, utcnow


def _uuid() -> str:
    return uuid.uuid4().hex


class MandateKind(StrEnum):
    CHECKOUT = "checkout"
    PAYMENT = "payment"


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class HoldStatus(StrEnum):
    HELD = "held"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class CheckoutState(StrEnum):
    QUOTED = "quoted"
    SIGNED = "signed"
    COMPLETING = "completing"
    AWAITING_PAYMENT = "awaiting_payment"
    COMPLETED = "completed"
    REFUSED = "refused"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


class EscalationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    VOIDED = "voided"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class RefundStatus(StrEnum):
    CREATED = "created"
    PROCESSED = "processed"
    FAILED = "failed"


class DisputeOutcome(StrEnum):
    OPEN = "open"
    CONTESTED = "contested"
    REFUNDED = "refunded"


class ConnectionScope(StrEnum):
    """Which surface a connection token addresses.

    A buyer connection lets somebody's own agent shop here; a merchant connection lets it run the
    shop. Neither carries purchasing authority, which stays with the credential chain.
    """

    BUYER = "buyer"
    MERCHANT = "merchant"


class NotificationKind(StrEnum):
    PURCHASE_COMPLETED = "purchase_completed"
    PURCHASE_REFUSED = "purchase_refused"
    PURCHASE_COMPENSATED = "purchase_compensated"


class NotificationStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class BuyerRunStatus(StrEnum):
    PLANNING = "planning"
    QUOTING = "quoting"
    PRESENTING = "presenting"
    AWAITING_PAYMENT = "awaiting_payment"
    COMPLETED = "completed"
    REFUSED = "refused"
    ESCALATED = "escalated"
    COMPENSATED = "compensated"
    ERROR = "error"


class Product(Base):
    """A catalog item with machine-readable purchase constraints and live stock."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    # What an agent or a person is shown. Part of the item's public description, so it is carried
    # into the evidence packet's catalog snapshot along with the price: reconstructing what the
    # buyer was shown means the picture too, not only the number.
    image_url: Mapped[str] = mapped_column(Text, default="")
    image_alt: Mapped[str] = mapped_column(String(256), default="")
    category: Mapped[str] = mapped_column(String(64), index=True)
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    stock_total: Mapped[int] = mapped_column(Integer, default=0)

    min_order_quantity: Mapped[int] = mapped_column(Integer, default=1)
    max_order_quantity: Mapped[int] = mapped_column(Integer, default=10)
    returnable: Mapped[bool] = mapped_column(Boolean, default=True)
    return_window_days: Mapped[int] = mapped_column(Integer, default=7)
    age_restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    region_lock: Mapped[list[str]] = mapped_column(JSONB, default=list)
    perishable: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("price_minor >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("stock_total >= 0", name="ck_products_stock_non_negative"),
    )


class AgentIdentity(Base):
    """A known agent, its merchant-set limits and its kill switch."""

    __tablename__ = "agent_identities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    key_thumbprint: Mapped[str] = mapped_column(String(64), index=True)
    public_jwk: Mapped[dict[str, Any]] = mapped_column(JSONB)
    issuer_id: Mapped[str] = mapped_column(String(128), index=True)
    tier: Mapped[str] = mapped_column(String(32), default="unverified")

    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    max_spend_per_window_minor: Mapped[int] = mapped_column(BigInteger, default=20_000_000)
    max_transactions_per_window: Mapped[int] = mapped_column(Integer, default=50)
    allowed_categories: Mapped[list[str]] = mapped_column(JSONB, default=list)
    blocked_categories: Mapped[list[str]] = mapped_column(JSONB, default=list)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class OpenMandate(Base):
    """An open Checkout or Payment Mandate signed by the human, with its consumption state."""

    __tablename__ = "open_mandates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    digest: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    sd_jwt: Mapped[str] = mapped_column(Text)
    claims: Mapped[dict[str, Any]] = mapped_column(JSONB)

    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    key_thumbprint: Mapped[str] = mapped_column(String(64), index=True)
    issuer_id: Mapped[str] = mapped_column(String(128), index=True)
    tier: Mapped[str] = mapped_column(String(32))

    cap_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    committed_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)

    not_before: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("committed_minor >= 0", name="ck_mandates_committed_non_negative"),
    )


class BudgetReservation(Base):
    """Reserve before commitment. Reservations expire so an abandoned attempt frees budget."""

    __tablename__ = "budget_reservations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    mandate_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=ReservationStatus.RESERVED, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_reservations_amount_positive"),
        Index("ix_reservations_mandate_status", "mandate_id", "status"),
    )


class InventoryHold(Base):
    """Stock held for a cart. Quota-limited per agent so holds cannot exhaust inventory."""

    __tablename__ = "inventory_holds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    checkout_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=HoldStatus.HELD, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_holds_quantity_positive"),
        Index("ix_holds_product_status", "product_id", "status"),
        Index("ix_holds_agent_status", "agent_id", "status"),
    )


class CredentialNonce(Base):
    """Replay store. A credential digest may be presented once."""

    __tablename__ = "credential_nonces"

    digest: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64))
    seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class PolicyTerms(Base):
    """Merchant policy terms, addressed by content hash and signed by the merchant key."""

    __tablename__ = "policy_terms"

    content_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    signed_jwt: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class CheckoutSession(Base):
    """A quote, the stock it holds, and the merchant-signed Checkout it commits to."""

    __tablename__ = "checkout_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(24), default=CheckoutState.QUOTED, index=True)

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    policy_hash: Mapped[str] = mapped_column(String(128))

    checkout: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checkout_jwt: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkout_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    # A snapshot sufficient to reconstruct what the buyer was shown. A reference to a mutable
    # product row would not be a snapshot.
    catalog_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cart_fingerprint: Mapped[str] = mapped_column(String(128), index=True)

    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payment_mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class Verdict(Base):
    """A policy decision. Money never moves without one of these recorded first."""

    __tablename__ = "verdicts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    checkout_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    action: Mapped[str] = mapped_column(String(32), index=True)
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    signed_jwt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class SpendEvent(Base):
    """Committed spend, used for rolling velocity and structuring windows."""

    __tablename__ = "spend_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class Escalation(Base):
    """A question put to the human. Unanswered means denied, never approved."""

    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    checkout_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)

    constraint_text: Mapped[str] = mapped_column(Text)
    raised_reason: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    # The approval covers exactly the cart it was raised for; any change voids it.
    cart_fingerprint: Mapped[str] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(16), default=EscalationStatus.PENDING, index=True)
    deadline_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    channel_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class EscalationResponse(Base):
    """Every inbound answer, including the late and duplicate ones that were ignored."""

    __tablename__ = "escalation_responses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    escalation_id: Mapped[str] = mapped_column(String(64), index=True)
    answer: Mapped[str] = mapped_column(String(16))
    accepted: Mapped[bool] = mapped_column(Boolean)
    ignored_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    checkout_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    # Enforces the ordering rule in code and in the schema: no payment row without a verdict.
    verdict_id: Mapped[str] = mapped_column(String(64), index=True)

    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.CREATED, index=True)

    captured_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    gateway_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    payment_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=RefundStatus.CREATED, index=True)
    compensating: Mapped[bool] = mapped_column(Boolean, default=False)
    gateway_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class PaymentException(Base):
    """A disagreement between the local record and Razorpay. Recorded, never silently corrected."""

    __tablename__ = "payment_exceptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    local_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    gateway_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class IdempotencyKey(Base):
    """Replayed responses for retried state-changing calls, so a retry cannot double charge."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(128))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class EvidencePacket(Base):
    """Append-only, hash-chained. A database trigger blocks UPDATE and DELETE."""

    __tablename__ = "evidence_packets"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    packet_id: Mapped[str] = mapped_column(String(64), unique=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    prev_hash: Mapped[str] = mapped_column(String(128))
    entry_hash: Mapped[str] = mapped_column(String(128), unique=True)
    signature: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    claim: Mapped[str] = mapped_column(Text)
    claimed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    recommendation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    strength_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    representment: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    outcome: Mapped[str] = mapped_column(String(16), default=DisputeOutcome.OPEN, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (UniqueConstraint("correlation_id", "claimed_at", name="uq_dispute_claim"),)


class AgentConnection(Base):
    """A connection somebody creates so their own agent can transact here.

    Only the token's digest is stored, so a leaked database row cannot be replayed as a token. The
    scope decides which surface the token addresses; it never confers purchasing authority, which
    comes from the credential chain alone.
    """

    __tablename__ = "agent_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(16), default=ConnectionScope.BUYER, index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    whatsapp_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    notify_completed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_refused: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    @property
    def live(self) -> bool:
        return self.revoked_at is None


class NotificationLog(Base):
    """Every outbound purchase receipt attempt, delivered or not.

    A delivery failure is recorded rather than retried into silence: the human not hearing about a
    purchase is itself a fact the merchant should be able to show.
    """

    __tablename__ = "notification_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    route: Mapped[str] = mapped_column(String(64), default="interactive")
    to_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class BuyerRun(Base):
    """One buyer-console purchase attempt, from natural-language prompt to recorded outcome."""

    __tablename__ = "buyer_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    prompt: Mapped[str] = mapped_column(Text)
    planner: Mapped[str] = mapped_column(String(32), default="rule-based")
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=BuyerRunStatus.PLANNING, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    checkout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_packet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class BuyerRunEvent(Base):
    """One line of the buyer agent's log, as the console renders it live."""

    __tablename__ = "buyer_run_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(8), default="info")
    step: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_buyer_run_event_seq"),)
