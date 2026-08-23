"""Pydantic models mirroring the published AP2 and UCP schemas.

Field names follow the vendored JSON Schemas exactly. Where a model is looser than the schema
(``extra="allow"`` on the UCP Checkout, which the schema declares ``additionalProperties: true``)
that mirrors the schema rather than relaxing it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ap2.vocabulary import (
    CheckoutStatus,
    Frequency,
    ReceiptStatus,
    TotalType,
    Vct,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Merchant(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    website: str | None = None


class Amount(BaseModel):
    model_config = ConfigDict(extra="allow")

    amount: int = Field(description="minor units, ISO 4217")
    currency: str


class PaymentInstrument(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    description: str | None = None


class Pisp(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str | None = None


class Item(BaseModel):
    """A line item's product details. The published schema requires a unit price."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    price: int = Field(ge=0, description="unit price in minor units")
    image_url: str | None = None


class AcceptableItem(BaseModel):
    """An item permitted by a line-item requirement in an open Checkout Mandate.

    Deliberately not the same type as Item: the open mandate constrains which SKUs may appear and
    carries no price, because the price is the merchant's to set at quote time.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    title: str


class Total(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: TotalType | str
    amount: int
    display_text: str | None = None


class LineItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    item: Item
    quantity: int = Field(ge=1)
    totals: list[Total]
    parent_id: str | None = None


class Link(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    url: str


class Checkout(BaseModel):
    """The merchant-signed UCP Checkout. This is what the merchant commits to fulfil."""

    model_config = ConfigDict(extra="allow")

    id: str
    line_items: list[LineItem]
    status: CheckoutStatus | str
    currency: str
    totals: list[Total]
    links: list[Link] = Field(default_factory=list)
    merchant: Merchant | None = None
    buyer: dict[str, Any] | None = None
    messages: list[dict[str, Any]] | None = None
    expires_at: str | None = None
    continue_url: str | None = None

    def total_minor(self) -> int:
        for total in self.totals:
            if str(total.type) == TotalType.TOTAL:
                return total.amount
        raise ValueError("checkout totals must contain exactly one entry of type 'total'")


# --- Open Checkout Mandate constraints ---------------------------------------------------------


class AllowedMerchantsConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["checkout.allowed_merchants"] = "checkout.allowed_merchants"
    allowed: list[Merchant]


class LineItemRequirement(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    acceptable_items: list[AcceptableItem]
    quantity: int = Field(gt=0)


class LineItemsConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["checkout.line_items"] = "checkout.line_items"
    items: list[LineItemRequirement] = Field(min_length=1)


class NaturalLanguageConstraint(BaseModel):
    """Dwarpal extension. Never evaluated by the deterministic kernel."""

    model_config = ConfigDict(extra="allow")

    type: Literal["dwarpal.natural_language"] = "dwarpal.natural_language"
    text: str


class OpenCheckoutMandate(BaseModel):
    model_config = ConfigDict(extra="allow")

    vct: Literal[Vct.OPEN_CHECKOUT_MANDATE] = Vct.OPEN_CHECKOUT_MANDATE
    constraints: list[dict[str, Any]]
    cnf: dict[str, Any]
    iat: int | None = None
    exp: int | None = None


class ClosedCheckoutMandate(BaseModel):
    model_config = ConfigDict(extra="allow")

    vct: Literal[Vct.CLOSED_CHECKOUT_MANDATE] = Vct.CLOSED_CHECKOUT_MANDATE
    checkout_jwt: str
    checkout_hash: str
    iat: int | None = None
    exp: int | None = None


# --- Payment Mandate constraints ---------------------------------------------------------------


class AmountRangeConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.amount_range"] = "payment.amount_range"
    currency: str
    max: int
    min: int | None = None


class BudgetConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.budget"] = "payment.budget"
    max: float
    currency: str


class AgentRecurrenceConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.agent_recurrence"] = "payment.agent_recurrence"
    frequency: Frequency
    max_occurrences: int | None = None


class AllowedPayeesConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.allowed_payees"] = "payment.allowed_payees"
    allowed: list[Merchant]


class AllowedInstrumentsConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.allowed_payment_instruments"] = "payment.allowed_payment_instruments"
    allowed: list[PaymentInstrument]


class AllowedPispsConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.allowed_pisps"] = "payment.allowed_pisps"
    allowed: list[Pisp]


class ExecutionDateConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.execution_date"] = "payment.execution_date"
    not_before: str | None = None
    not_after: str | None = None


class PaymentReferenceConstraint(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["payment.reference"] = "payment.reference"
    conditional_transaction_id: str


class OpenPaymentMandate(BaseModel):
    model_config = ConfigDict(extra="allow")

    vct: Literal[Vct.OPEN_PAYMENT_MANDATE] = Vct.OPEN_PAYMENT_MANDATE
    constraints: list[dict[str, Any]]
    cnf: dict[str, Any]
    payee: Merchant | None = None
    payment_amount: Amount | None = None
    payment_instrument: PaymentInstrument | None = None
    pisp: Pisp | None = None
    execution_date: str | None = None
    risk_data: dict[str, Any] | None = None
    iat: int | None = None
    exp: int | None = None


class ClosedPaymentMandate(BaseModel):
    model_config = ConfigDict(extra="allow")

    vct: Literal[Vct.CLOSED_PAYMENT_MANDATE] = Vct.CLOSED_PAYMENT_MANDATE
    transaction_id: str
    payee: Merchant
    payment_amount: Amount
    payment_instrument: PaymentInstrument
    pisp: Pisp | None = None
    execution_date: str | None = None
    risk_data: dict[str, Any] | None = None
    iat: int | None = None
    exp: int | None = None


# --- Receipts ----------------------------------------------------------------------------------


class CheckoutReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: ReceiptStatus
    iss: str
    iat: int
    reference: str
    order_id: str | None = None
    error: str | None = None
    error_description: str | None = None


class PaymentReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: ReceiptStatus
    iss: str
    iat: int
    reference: str
    payment_id: str
    psp_confirmation_id: str | None = None
    network_confirmation_id: str | None = None
    error: str | None = None
    error_description: str | None = None
