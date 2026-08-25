# Dwarpal Merchant Policy Terms

These terms govern purchases made by an autonomous agent on behalf of a human
principal. An agent must acknowledge the content hash of the terms that were live when its cart
was quoted. A checkout presenting any other hash is refused.

## 1. Who may transact

An agent may browse, search, request quotes and hold stock without presenting credentials. An
agent that cannot present an acceptable credential may complete a purchase only below the
published unverified ceiling, and may not purchase any age restricted or otherwise restricted
item at any value.

## 2. Authority

The merchant verifies that a closed Checkout Mandate binds to a Checkout the merchant itself
signed, and that the constraints carried by the open Checkout Mandate are satisfied. A purchase
that exceeds the authority the human granted is refused, and the refusal is recorded.

## 3. Price and availability

A quote fixes the price and holds the stock for the stated period. The merchant commits to fulfil
at the quoted stock keeping unit, price and shipping. Outside that period the quote must be
renewed. Prices are stated in the minor unit of the stated currency.

## 4. Stock holds

Holding stock is limited per agent. Holds expire. An expired hold releases the stock immediately
and confers no right to the item or to the quoted price.

## 5. Returns

Return eligibility and the return window are published per item in the machine readable catalog
and form part of these terms for each item purchased. Perishable and age restricted items are not
returnable.

## 6. Restricted items

Age restricted items require a credential from an issuing authority whose tier permits them.
Region locked items are not sold into the regions listed against the item.

## 7. Revocation

A human principal may revoke an open mandate at any time. Revocation is checked immediately
before money moves. If a revocation is received after capture has already occurred, the merchant
issues a compensating refund of the full captured amount without requiring a request, records the
outcome under a distinct status, and files the transaction evidence regardless.

## 8. Evidence

Each transaction produces an evidence record containing the credentials presented, the catalog
state and prices live at the time of the quote, the acknowledged hash of these terms, every policy
decision with its reason code, any escalation to the human principal, the timing of each step, and
the payment and refund records. Records are append only and independently verifiable.

## 9. Disputes

Where a purchase is disputed, the merchant will produce the evidence record described above. Where
that record does not adequately establish that the agent acted inside the authority the human
granted, the merchant will refund rather than contest.

## 10. Escalation

Where a constraint cannot be decided mechanically, the merchant may contact the human principal
for a decision. If no answer is received before the stated deadline, the purchase is refused. A
lack of response is never treated as approval.
