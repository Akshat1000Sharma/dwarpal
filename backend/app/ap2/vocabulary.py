"""AP2 wire vocabulary.

Current specification vocabulary only. The "Intent Mandate, Cart Mandate, Payment Mandate" triad
is the September 2025 launch language and is out of date; it must not appear anywhere.

Every constant here is taken from the published JSON Schemas vendored under ``schemas/``.
"""

from __future__ import annotations

from enum import StrEnum

AP2_PROTOCOL_VERSION = "0.2"
AP2_SPEC_URL = "https://ap2-protocol.org/"
AP2_SCHEMA_REVISION = "e1ea56db72a6385bce3e5c1112b3a56ce60acb43"

SD_JWT_TYP = "dc+sd-jwt"
CHECKOUT_JWT_TYP = "ap2-checkout+jwt"
RECEIPT_JWT_TYP = "ap2-receipt+jwt"
VERDICT_JWT_TYP = "dwarpal-verdict+jwt"
EVIDENCE_JWT_TYP = "dwarpal-evidence+jwt"
POLICY_TERMS_JWT_TYP = "dwarpal-policy+jwt"


class Vct(StrEnum):
    """Verifiable Credential Type claim values.

    The trailing digit is not a Dwarpal version number and must not be stripped. Each value is
    pinned by ``const`` in the vendored schema for that credential under ``schemas/ap2/``, so a
    credential carrying a shortened vct fails validation on issue and on accept.
    """

    OPEN_CHECKOUT_MANDATE = "mandate.checkout.open.1"
    CLOSED_CHECKOUT_MANDATE = "mandate.checkout.1"
    OPEN_PAYMENT_MANDATE = "mandate.payment.open.1"
    CLOSED_PAYMENT_MANDATE = "mandate.payment.1"


class CheckoutConstraint(StrEnum):
    ALLOWED_MERCHANTS = "checkout.allowed_merchants"
    LINE_ITEMS = "checkout.line_items"


class PaymentConstraint(StrEnum):
    AGENT_RECURRENCE = "payment.agent_recurrence"
    ALLOWED_PAYEES = "payment.allowed_payees"
    ALLOWED_PAYMENT_INSTRUMENTS = "payment.allowed_payment_instruments"
    ALLOWED_PISPS = "payment.allowed_pisps"
    AMOUNT_RANGE = "payment.amount_range"
    BUDGET = "payment.budget"
    EXECUTION_DATE = "payment.execution_date"
    REFERENCE = "payment.reference"


class Frequency(StrEnum):
    ON_DEMAND = "ON_DEMAND"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUALLY = "ANNUALLY"


class CheckoutStatus(StrEnum):
    """UCP checkout lifecycle, from dev.ucp.shopping.checkout."""

    INCOMPLETE = "incomplete"
    REQUIRES_ESCALATION = "requires_escalation"
    READY_FOR_COMPLETE = "ready_for_complete"
    COMPLETE_IN_PROGRESS = "complete_in_progress"
    COMPLETED = "completed"
    CANCELED = "canceled"


class TotalType(StrEnum):
    SUBTOTAL = "subtotal"
    DISCOUNT = "discount"
    ITEMS_DISCOUNT = "items_discount"
    FULFILLMENT = "fulfillment"
    TAX = "tax"
    FEE = "fee"
    TOTAL = "total"


class ReceiptStatus(StrEnum):
    SUCCESS = "Success"
    ERROR = "Error"


# Constraint types Dwarpal evaluates deterministically. Anything outside this set is reported as
# unresolved and routed to the semantic path; it is never treated as satisfied.
DETERMINISTIC_CONSTRAINTS: frozenset[str] = frozenset(
    {
        CheckoutConstraint.ALLOWED_MERCHANTS,
        CheckoutConstraint.LINE_ITEMS,
        PaymentConstraint.AGENT_RECURRENCE,
        PaymentConstraint.ALLOWED_PAYEES,
        PaymentConstraint.ALLOWED_PAYMENT_INSTRUMENTS,
        PaymentConstraint.ALLOWED_PISPS,
        PaymentConstraint.AMOUNT_RANGE,
        PaymentConstraint.BUDGET,
        PaymentConstraint.EXECUTION_DATE,
        PaymentConstraint.REFERENCE,
    }
)

# A Dwarpal extension: the open Checkout Mandate may carry constraints the human expressed in
# prose. These are never evaluated by the kernel.
#
# They live in their own top-level claim rather than inside the AP2 "constraints" array, because
# the published open_checkout_mandate schema constrains that array to the two AP2 constraint types
# and a foreign entry would make the credential non-conformant. Keeping the extension outside it
# means every credential Dwarpal issues and accepts still validates against the published schema.
NATURAL_LANGUAGE_CONSTRAINT = "dwarpal.natural_language"
EXTENSION_CONSTRAINTS_CLAIM = "dwarpal_constraints"
