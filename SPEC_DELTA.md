# Where the code differs from the plan, and which is right

`IMPLEMENTATION_PLAN.md` is the specification. It was written before any code existed and it
deliberately contains no endpoint paths, no payload shapes and no schemas, because those were
meant to be decided while building. This document is the other half of that bargain: every place
the built system diverges from what the plan says, why, and an honest judgement about which is
better.

Some of these are the plan being right and the code catching up. Most are the code being right,
because a specification written in advance cannot know what a real JSON Schema will reject or what
PostgreSQL will do under contention. Two are still open.

## Summary

| # | Area | Plan | Code | Better | Why, in one line |
|---|---|---|---|---|---|
| 1 | Natural-language constraints | inside the mandate's `constraints` array | a separate `dwarpal_constraints` claim | **code** | the published schema rejects a foreign entry in that array |
| 2 | Semantic outcome on a clean pass | implied approve-through | `no_violation_found` still escalates | **code** | the model has no approval outcome at all, so it cannot be jailbroken into one |
| 3 | WhatsApp | escalation only | escalation plus purchase receipts | **code** | a human told only about ambiguities never learns what was bought |
| 4 | Reports storage | one reports directory | two separate databases | **code** | both runs truncate, so one database destroyed the first chain |
| 5 | Merchant control plane | not mentioned | `X-Merchant-Token` required, closed when unset | **code** | the documented runbook tunnels this port to the public internet |
| 6 | Buyer-facing UI | "agents never touch the dashboard" | a separate buyer console | **code** | the purchase path stays headless; the console is a client of it |
| 7 | Evidence appends | append-only, hash chained | the same, plus an advisory lock | **code** | concurrent appends forked the chain and answered agents with a 500 |
| 8 | Budget locking | "real row-level locking" | row lock **plus a refreshed read** | **code** | the lock was right and the value read under it was stale |
| 9 | Corpus as data | "data rather than hand-written test functions" | YAML corpus **plus** a coded scenario suite | **code** | some properties are about timing and concurrency, which data cannot express |
| 10 | Attack corpus scope | "expressed as data" | in-process YAML plus HTTP-level suites | **code** | a verification step only ever called directly proves less than one an attacker could reach |
| 11 | Login | not mentioned | a passwordless persona chooser | **code** | it removes the terminal from the demo without claiming a boundary that does not exist |
| 12 | Model use | "Gemini is used in exactly one place" | two places, on opposite sides of the counter | **plan, restated** | the invariant is about the merchant's kernel, and that still holds absolutely |
| 13 | Human-present flow | "may be supported if it falls out naturally" | not implemented | **plan** | it did not fall out naturally, and pretending otherwise would be the dishonest option |
| 14 | Reference shopping agent | "launched by a single documented command" | not run against | **plan** | this is a real gap, stated as one |

---

## 1. Natural-language constraints live outside the AP2 constraints array

**Plan.** Section 5 says constraints expressed in natural language are handled by the semantic
path, implying they arrive alongside the numeric ones.

**Code.** They are carried in a top-level `dwarpal_constraints` claim
(`backend/app/ap2/vocabulary.py`), never inside `constraints`.

**Which is better: the code.** The vendored `open_checkout_mandate.json` constrains the
`constraints` array to exactly two AP2 types via `anyOf`. A `dwarpal.natural_language` entry inside
it makes the whole credential fail schema validation, which would have quietly withdrawn the
project's only concrete conformance claim. Keeping the extension outside the array means every
credential Dwarpal issues and accepts still validates against the published schemas, which is
exactly what the plan asks for two sections earlier.

## 2. A clean semantic pass still escalates

**Plan.** Section 7 says the model may only deny or escalate. It does not say what happens when the
model finds nothing wrong.

**Code.** `no_violation_found` produces `ESCALATE`, not an approval. There is no function anywhere
in the codebase that converts model output into an approval.

**Which is better: the code.** The plan's wording could be satisfied by a system where
`no_violation_found` lets the purchase through, and that system is breakable: anything that makes
the model say "no violation" buys something. Making the outcome type have no approval member means
a compromised, jailbroken or hallucinating model can only ever cost the buyer a question to their
human. The deliberate price is that a prose-constrained cart the model cleared still goes to the
human, and `s07.a_clean_cart_under_a_prose_constraint_still_asks` exists to keep that price visible.

## 3. WhatsApp carries receipts as well as escalations

**Plan.** Section 8 describes WhatsApp only as the escalation channel.

**Code.** There is a second, one-way message: an agent bought this on your behalf, or an agent's
purchase was refused, or the money was given back.

**Which is better: the code.** An escalation only fires when the kernel cannot decide. Under the
plan as written, the overwhelmingly common case, an agent buying something entirely within its
authority, tells the human nothing at all. The person whose money moved finds out by checking a
dashboard, which is not how anybody finds out about a payment.

Two rules keep it from becoming noise. A refusal is only sent to somebody who registered the agent,
because messaging the merchant's own phone on every forged credential turns a notification into an
alarm that gets muted. And delivery happens on a bounded worker with its own session, never on the
request that decided the money.

## 4. Reports write to two databases, not one

**Plan.** Section 13 says both reports are written to a reports directory.

**Code.** The scorecard and the dispute report each get their own `<DB_NAME>_reports` and
`<DB_NAME>_disputes` database.

**Which is better: the code.** Every report run starts by truncating, so sharing one database meant
generating both reports left only the second one's evidence chain behind. The offline verification
step in CI then had nothing to verify. The artifacts still land in one reports directory, as the
plan asks; only the working storage is separate.

## 5. The merchant control plane requires a token

**Plan.** Section 14 describes the dashboard surface and does not mention authentication.

**Code.** Every `/merchant` route requires `X-Merchant-Token`, and an unset token refuses every
request rather than serving the surface open.

**Which is better: the code.** These endpoints revoke a human's mandate, stop an agent, widen a
spend limit and close a money discrepancy. The plan's own runbook tunnels this port to the public
internet so Razorpay and Meta webhooks can arrive. Serving it open by default would put a stranger
one URL away from the kill switch, and failing closed when the token is unset means a
misconfiguration cannot quietly publish it.

## 6. There is a buyer console, and agents still never touch the dashboard

**Plan.** Section 14 opens with "the frontend is the merchant's view. It is not the transaction
path; agents never touch it."

**Code.** There is a second console where a person types a sentence and watches an agent buy
something.

**Which is better: the code, and the plan's invariant is intact.** The buyer console is a client of
the same modules an external agent reaches over HTTP: the same quote, the same
`app/harness/factory.py`, the same `app/checkout/complete.py`. It short-circuits nothing, so a run
in the console and a run from a shell produce the same verdict for the same cart. What the plan
was protecting is that the purchase path must be completable by a machine with no human-facing web
UI in it, and that is still true and still exercised by `interop/run_interop.py` and the scenario
suite on every push.

What the plan did not anticipate is that a system whose only demonstration is a terminal command is
one nobody will run.

## 7. Evidence appends are serialised, not merely append-only

**Plan.** Section 10 requires packets to be append-only and hash chained.

**Code.** Both, plus a PostgreSQL advisory lock taken before the chain head is read.

**Which is better: the code, and this was a real defect.** A hash chain is serial by definition:
each entry commits to its predecessor. Two concurrent checkouts read the same head, computed the
same sequence number and the same `prev_hash`, and one lost on the primary key with an HTTP 500.
The agent that lost was refused or charged with no packet filed, which is the one outcome the
Evidence Locker exists to prevent. Found by `s11`, the mixed-traffic soak, at twenty-five
occurrences in a twenty-second run.

The lock is transaction-scoped, and that is only safe because nothing slow happens after an append.
The WhatsApp receipt that used to sit there was moved off the request for exactly this reason.

## 8. The budget lock needed a refreshed read

**Plan.** Section 6 is emphatic: "This must use real row-level locking, not an application-level
check-then-write."

**Code.** `SELECT ... FOR UPDATE`, and `populate_existing=True` so the locked read is actually used.

**Which is better: the code, and this was the worst defect found.** The lock was correct. The value
read under it was not: SQLAlchemy returns the copy already in the session's identity map rather
than the row it just locked, so a checkout that had loaded the mandate earlier, which every real
checkout does, decided against a balance from before every other session's spend. Five concurrent
draws against a two-unit cap all settled, and the mandate recorded 325,000 paise committed against
a cap of 130,000.

The existing concurrency fuzz missed it because it reserved from a fresh session every time, and a
real checkout never does. `tests/test_concurrency.py::test_the_cap_holds_when_the_mandate_was_already_loaded`
is the regression, and it fails against the old code: thirty draws granted where ten were allowed.

This is the plan being exactly right and the implementation being subtly wrong in the one way the
plan warned about.

## 9 and 10. The corpus is data, and there is also a coded scenario suite

**Plan.** Section 12 says the adversarial corpus is "expressed as data rather than as hand-written
test functions".

**Code.** The YAML corpus is unchanged and still drives the scorecard. Alongside it,
`backend/scenarios/` is eleven coded suites driven over HTTP against a running merchant.

**Which is better: the code.** The plan's reasoning is sound for attack families: a new one should
be a YAML file, not a code change, and it still is. But several of the properties the plan itself
demands cannot be expressed as a row of data:

- "two concurrent authorisations must never both succeed" is about two requests overlapping in time
- "one wins and the other receives a structured response" is about who loses a race
- "a retried request must never produce a second charge" is about the same bytes arriving twice
- "the concurrency fuzz test passes and demonstrably fails against a naive implementation" needs a
  naive implementation to compare against

The corpus runs in-process. The suite runs over the wire, and that difference is not cosmetic: a
verification step only ever called directly proves less than one an attacker could actually reach.
The suite found four defects the corpus could not, all listed above.

## 11. There is a login page, and it says it is not authentication

**Plan.** Does not mention one.

**Code.** `/login` offers two buttons and sets a cookie.

**Which is better: the code, narrowly.** It removes the terminal from the first five minutes, which
matters more than it sounds. The risk was claiming a security boundary that does not exist, so the
page states in plain text that it is a view selector, that there is no password, and that the real
boundaries are the credential chain and the merchant token. Every route stays reachable by URL,
because the cookie is a preference and dressing it up as a permission would be the dishonest option.

## 12. A model is used in two places, on opposite sides of the counter

**Plan.** Section 7: "Gemini is used in exactly one place."

**Code.** Two. The merchant's semantic check, exactly as specified. And the buyer's shopping agent,
which turns a sentence into a cart.

**Which is better: the plan, correctly restated.** The invariant the plan is protecting is that no
model touches a money decision, and that is unchanged and structurally enforced:
`tests/test_kernel_isolation.py` walks the transitive import closure of `app/kernel/` and fails the
build if `app.buyer`, `app.semantic`, `google` or `httpx` is reachable from it.

The buyer's agent is on the other side of the counter. It is the thing the merchant is defending
against, and it being model-driven is the point: choosing what to buy is a judgement call where
being wrong costs the buyer a wrong item, while deciding whether to allow it is not, because being
wrong there costs somebody else their money. Whatever the planner proposes is re-read from the
catalog before anything is quoted, so a hallucinated SKU or price never reaches the wire.

The plan's sentence should read: a model is used in exactly one place on the merchant's side.

## 13. The human-present flow is not implemented

**Plan.** Section 3: "The human-present flow may be supported if it falls out naturally, but it is
not the objective."

**Code.** Not implemented.

**Which is better: the plan.** It did not fall out naturally, and the conformance matrix says so
rather than implying partial support.

## 14. The upstream reference shopping agent has not been run against Dwarpal

**Plan.** Section 16 makes it a condition of done: "The published AP2 reference shopping agent
transacts against Dwarpal successfully, launched by a single documented command."

**Code.** Not achieved.

**Which is better: the plan, and this is an open gap.** `shopping_agent_v2` is a Google ADK
application that needs `uv` and a Google API key and speaks A2A rather than plain HTTP. Dwarpal
exposes an MCP server presenting the catalog, the policy terms and the quote endpoint in the shape
that agent expects, but the two have not been run against each other here.

What is verified instead: `interop/run_interop.py` plays the Trusted Surface, the Shopping Agent
and the mocked Credential Provider against a running Dwarpal over HTTP, and every credential it
puts on the wire is validated against the published AP2 JSON Schemas before it is sent, so the run
cannot pass by feeding Dwarpal something the specification would reject. That is a weaker claim
than the plan asked for, and the README's conformance matrix states it as the weaker claim rather
than rounding it up.

---

## Things the plan asked for that are done exactly as written

Listed so the table above is not mistaken for the whole picture.

- Seven ordered verification steps, refusing at the first failure, with the step recorded.
- A deterministic kernel that no model can reach, enforced by an import-closure test rather than
  by convention.
- Reason codes as a closed enumeration, every verdict signed, every refusal carrying one.
- Reserve before commitment, with expiry, under a real row lock.
- Inventory holds, per-agent hold quotas, and a substitute offered when the last unit is gone.
- Structuring detection over rolling per-agent and per-mandate windows.
- A per-agent kill switch that stops one agent without affecting any other.
- Revocation checked immediately before execution, and compensated automatically when it lands
  after capture.
- Escalation deadlines that fail closed, answered once, void if the cart changes.
- Webhook signatures verified over the raw bytes before parsing, on both channels.
- Payment state reconciled from Razorpay, with disagreements filed as exceptions rather than
  silently corrected.
- Append-only enforced by a database trigger, not by application convention.
- A standalone verifier that imports nothing from the application.
- A dispute responder that says when to refund rather than contest.
- Both scorecard numbers reported together, always, with misses named.
- Configuration validated at startup, refusing to run against a live Razorpay key.
- Correlation identifiers threaded through every log line and stored record.
