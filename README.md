# Dwarpal

The AP2 merchant endpoint for Razorpay. Dwarpal makes a Razorpay merchant transactable by an AI
buyer agent, end to end, and keeps the evidence that defends the transaction if it is later
disputed.

```bash
docker compose up -d                                    # PostgreSQL
cd backend && uvicorn main:app --reload                  # the merchant
cd frontend && npm run dev                               # the two consoles
cd backend && python scenarios/run_suite.py --profile demo   # fill it with data
```

Then open http://localhost:3000 and pick a door.

## The problem

In the AP2 human-not-present flow, the merchant is obliged to verify that a closed Checkout
Mandate matches the current cart state, and that the constraints in the open Checkout Mandate have
actually been met. The specification assigns that duty to the merchant. It does not specify how to
discharge it.

The duty lands on the merchant because the merchant carries the loss. When an agent buys and the
human later disputes the purchase, there is no 3-D Secure record for a bot, no signed receipt and
no accepted evidence standard. Dwarpal is the gate that prevents the unauthorised purchase and the
evidence that defends the dispute when one is raised anyway.

## What it does

- Publishes an agent-readable catalog with machine-readable purchase constraints and
  merchant-signed policy terms.
- Verifies inbound agent credentials: signature chain, subject binding, issuer trust tier,
  validity window, replay, and the closed-against-open constraint check.
- Gates every money action through a deterministic policy kernel that no model can influence.
- Completes checkout headlessly against Razorpay test mode, with idempotency and verified
  webhooks.
- Writes a hash-chained evidence packet for every transaction, verifiable offline.
- Tells the human over WhatsApp what their agent bought, or why it was refused.
- Assembles dispute representments, and says when the evidence is too weak to contest.

Agents that cannot present acceptable credentials are not refused outright. They may browse,
quote and build a cart, but cannot check out above a configured ceiling or buy restricted
categories.

## The two consoles

`/` is a public page describing the system. `/login` offers two views, with no password: there is
nothing to authenticate against, and the page says so. Dwarpal's real boundaries are the credential
chain an agent presents and the merchant token the dashboard sends from the server.

| Console | Route | What it is for |
|---|---|---|
| Buyer | `/buyer` | Ask an agent to buy something, watch its log, pay with the test card |
| Merchant | `/merchant` | Traffic, verdicts, mandates, agent controls, evidence, disputes, scorecards |

### The buyer console

Type an instruction in plain language and press send. The console then does, in order, what any
external agent would do over HTTP:

1. Mints an agent identity and publishes its issuing authority's public key.
2. Reads the catalog and turns your sentence into a cart. Whatever the planner proposes is re-read
   from the catalog before anything is quoted, so a hallucinated SKU or price never reaches the
   wire.
3. Asks the merchant for a quote. Prices freeze, stock is held, and the merchant signs a Checkout
   it commits to fulfil.
4. Has a trusted surface sign the two open mandates, and the agent sign the two closed ones.
5. Posts all four to `/checkout/complete` and is judged.

Every step lands in a log you can watch live at `/buyer/runs/{id}`, with its own timing, the
credential sizes and digests, the kernel's verdict and reason code, and a link straight into the
evidence packet for that transaction.

**Paying.** When the kernel approves, a Razorpay order is created and the run sits at
`awaiting_payment`. Press **Pay with the test card** and Razorpay's hosted test-mode Checkout
opens. The card is Razorpay's published test card, shown on the page with copy buttons:

```
4111 1111 1111 1111    expiry 12/30    CVV 123
```

The result Razorpay hands back to the page is untrusted, so the server re-checks its HMAC over
`order_id|payment_id` before capturing anything. Dwarpal refuses to start against a live Razorpay
key, so nothing here can move real money.

`/buyer/setup` is the full guide: getting a connection, finding the merchant, browsing and quoting,
building the four mandates, reading a refusal without parsing prose, setting up automated payments,
and a paste-ready MCP config for pointing Claude at the catalog.

### Connecting your own agent

`/merchant/connections` mints a connection for an agent you run yourself, for buying or for
selling. You give it a label and a WhatsApp number; you get a token back exactly once, along with
the endpoints it addresses and a curl command that works.

| Scope | Reaches | Typical use |
|---|---|---|
| `buyer` | the agent surface: browse, search, quote, complete | your agent shops here |
| `merchant` | the control plane: verdicts, evidence, mandates, agent limits, kill switch | your agent runs the shop |

Send it as `X-Dwarpal-Connection` on every request. Only the token's SHA-256 digest is stored, so a
leaked database row cannot be replayed as a token and a lost token is replaced rather than
recovered. Revoking takes effect on the very next request.

**A connection grants no purchasing authority.** It answers two questions: whose agent is this, and
where do we tell them what it did. What an agent may buy comes from the mandates a human signed and
from nowhere else, so a stolen buyer token buys nothing. A merchant-scoped token does reach the
control plane, so treat it the way you would treat the shared merchant secret.

## Where the model is used, and where it is not

A model appears twice in this repository, on opposite sides of the counter, and the difference is
the whole design.

**The merchant's side.** The deterministic policy kernel makes every money decision. No model is
called on that path, and that is enforced structurally rather than by convention:
`tests/test_kernel_isolation.py` walks the transitive import closure of every module in
`app/kernel/` and fails the build if any model client or network client is reachable from it.

A model is consulted in exactly one place on this side: evaluating constraints expressed in natural
language, which arithmetic cannot check. Its authority is deliberately clipped. Two separate types
carry the invariant:

| Type | Members |
|---|---|
| `SemanticReply.verdict`, the wire model | `violates`, `no_violation_found` |
| `SemanticOutcome`, what the kernel sees | `DENY`, `ESCALATE` |

`violates` is the only input that produces `DENY`. Everything else, including
`no_violation_found`, an unparseable response, a timeout and any exception, produces `ESCALATE`.
There is no function in the codebase that turns model output into an approval, so a compromised or
jailbroken model degrades the system to asking the human more often, never to moving more money.
The deliberate consequence is that a natural-language constraint the model does not find violated
goes to the human rather than through.

**The buyer's side.** The shopping agent is model-driven on purpose. Choosing what to buy is a
judgement call where being wrong costs the buyer a wrong item. Deciding whether to allow it is not,
because being wrong there costs somebody else their money. The planner's output is treated as a
suggestion: every SKU is re-read from the catalog, quantities are clamped to each item's own
declared range, and the total is recomputed from merchant prices before anything is quoted.

If the model is rate limited or unreachable, the run does not fail. It falls back to a
deterministic catalog planner and says so in the log.

## Architecture

| Component | Responsibility |
|---|---|
| Catalog and discovery | Agent-readable inventory, purchase constraints, signed policy terms |
| Credential verification | AP2 mandate chain validation and constraint satisfaction |
| Policy kernel | Deterministic, reason-coded, signed verdicts on every money action |
| Semantic check | Natural-language constraint evaluation, deny or escalate only |
| Escalation | Human approve or deny over WhatsApp, with a deadline that fails closed |
| Notification | Purchase receipts: what an agent bought, or why it was refused |
| Checkout | Headless quote to fulfilment on Razorpay test mode |
| Evidence Locker | Hash-chained, offline-verifiable transaction records |
| Dispute responder | Representment assembly and contest-or-refund recommendation |
| Connections | Identity and delivery address for somebody else's agent |
| Buyer console | A demonstration client of the agent surface, short-circuiting nothing |
| Dashboard | Merchant view of traffic, verdicts, mandates, evidence and disputes |

## How a purchase is decided

Four stages, in order. Each can refuse. Only the policy kernel can approve on its own; the semantic
check and the human exist to resolve what the kernel could not.

```
  agent presents four mandates and a cart
                     |
                     v
  +==============================================================+
  |  1. VERIFICATION PIPELINE                                    |
  |  well formed and signed, subject binding, issuer trust,      |
  |  validity window, replay, checkout binding, constraints      |
  +==============================================================+
         |                                          |
   fails a step                              all seven pass
         |                                          |
         v                                          v
     REFUSED                          +==============================================+
   CRED_* reason code                 |  2. POLICY KERNEL                            |
   agent recorded as unknown,         |  kill switch, revocation, policy hash, item  |
   because identity was never         |  policy, tier ceiling, constraint            |
   established                        |  satisfaction, agent controls, structuring,  |
                                      |  budget. Deterministic. Never calls a model. |
                                      +==============================================+
                                          |              |                |
                                    breaks a rule   all clear      one constraint
                                          |              |          unresolved
                                          v              v                |
                                      REFUSED        APPROVED             |
                                                   money may move         v
                                               +===============================+
                                               |  3. SEMANTIC CHECK            |
                                               |  the unresolved constraint    |
                                               |  only. DENY or ESCALATE, and  |
                                               |  no approval member exists    |
                                               +===============================+
                                                     |                |
                                                "violates"      anything else,
                                                     |          including a clean
                                                     v          no_violation_found
                                                 REFUSED                |
                                                                        v
                                                            +======================+
                                                            |  4. HUMAN ESCALATION |
                                                            |  approve or deny over|
                                                            |  WhatsApp, against a |
                                                            |  deadline            |
                                                            +======================+
                                                              |        |        |
                                                         approves    denies   silence
                                                              |        |        |
                                                              v        v        v
                                                          APPROVED  REFUSED  REFUSED
```

Every uncertainty resolves towards refusing: an unparseable credential, an unknown authority, an
expired mandate, a constraint arithmetic cannot settle, a model that is unsure, a human who does not
answer, an unreachable gateway, an undeclared region for a region-locked item. None of them produce
an approval.

## The credentials an agent presents

AP2 separates two questions and answers each at two moments. The open mandates are the standing
authority, signed in advance by the human's trusted surface. The closed mandates are the claim about
this specific purchase, signed by the agent. The merchant checks that the open pair is genuine, that
the agent holds the key they were issued to, and that the closed pair fits inside the open pair.

```
   signed by the human's trusted surface     signed by the agent itself
   (standing authority, reusable)            (this purchase, single use)
   +-----------------------------+           +-----------------------------+
   |  OPEN CHECKOUT MANDATE      |           | CLOSED CHECKOUT MANDATE     |
   |  mandate.checkout.open.1    |  <-fits-  | mandate.checkout.1          |
   |  what may be bought:        |   inside  | what is being bought:       |
   |  merchants, line items,     |           | checkout_jwt, checkout_hash |
   |  amount range, recurrence   |           |                             |
   |  + cnf.jwk, the agent key   |           |                             |
   +-----------------------------+           +-----------------------------+
   +-----------------------------+           +-----------------------------+
   |  OPEN PAYMENT MANDATE       |           | CLOSED PAYMENT MANDATE      |
   |  mandate.payment.open.1     |  <-fits-  | mandate.payment.1           |
   |  how it may be paid:        |   inside  | how it is being paid:       |
   |  payees, instruments,       |           | transaction_id, payee,      |
   |  budget, execution window   |           | payment_amount, instrument  |
   |  + cnf.jwk, the agent key   |           |                             |
   +-----------------------------+           +-----------------------------+
```

The `cnf.jwk` claim names the key the mandate was issued to. The agent proves possession by signing
a key-binding JWT with the matching private half, which is what defeats a stolen mandate:
presenting a genuine credential issued to somebody else fails at `CRED_SUBJECT_MISMATCH`.

The trailing digit in each `vct` is not a Dwarpal version number and must not be stripped. It is
pinned by `const` in the vendored schema for that credential, so a shortened value fails validation
on issue and on accept.

## End to end

```
   HUMAN PRINCIPAL              AGENT                          DWARPAL
   sets the authority           transacts                      decides and records
       |                            |                               |
       | signs the open mandates    |                               |
       +--------------------------->|                               |
                                    |  browse, search, item detail  |
                                    |  with purchase constraints    |
                                    |------------------------------>|
                                    |  quote: prices frozen, stock  |
                                    |  held, Checkout signed by us  |
                                    |<------------------------------|
                                    |  presents the four mandates   |
                                    |------------------------------>|
                                    |                        1. verification
                                    |                        2. policy kernel
                                    |                        3. semantic check
       |<---------------------------|------------------------ 4. escalation
       |  approve or deny           |                               |
       +--------------------------->|                               |
                                    |                        5. Razorpay: order,
                                    |                           authorise, capture.
                                    |                           Never before a verdict
                                    |                               |
                                    |                        6. evidence packet:
                                    |                           append-only, hash
                                    |                           chained, verifiable
                                    |                           with the app stopped
       |<---------------------------|------------------------ 7. receipt over WhatsApp:
       |  what your agent bought    |                           bought, refused or
       |                            |                           reversed
                                    |                        8. dispute: score the
                                    |                           evidence, contest
                                    |                           or recommend refund
```

---

# Testing

Four layers, and they test different things on purpose. The fast suite proves each guarantee in
isolation. The corpus fires attack families as data. The scenario suite proves the same guarantees
hold through the real HTTP surface under concurrency. The soak proves they still hold at volume.

| Layer | Command | Size | What it is for |
|---|---|---|---|
| Unit and integration | `pytest` | 231 tests | Every guarantee, in isolation, in process |
| Adversarial corpus | `python -m app.cli reports` | 47 scenarios | Attack families as data, plus a matched benign corpus |
| Scenario suite | `python scenarios/run_suite.py` | 90 cases, 11 suites | The same guarantees over HTTP, under real concurrency |
| Soak | `pytest -m soak` | 9 cases | The same guarantees at volume |
| Interop | `python interop/run_interop.py` | 4 scenarios | A live AP2 client against a running merchant |

## Results, recorded

Every number below came from running the commands on this machine. Nothing here is estimated, and
the misses are named rather than summarised away.

### The fast suite

```
231 passed, 9 deselected in 118.21s
```

The suite requires PostgreSQL and fails with an instruction rather than skipping: the budget and
inventory guarantees are tested against real row-level locking, and a suite that quietly skipped
them would be dishonest. It also forces its own profile. `APP_ENV=testing` is what selects the
recording WhatsApp transport, the deterministic buyer planner and the stub payment gateway, so
`tests/conftest.py` sets it whatever a developer's `.env` says. Before that, a local `.env` carrying
`APP_ENV=development` made the suite try to reach Gemini, which passed in CI and failed on a
developer's machine.

### The attack scorecard

```
attack scorecard: 35/35 blocked, 0 missed, 0/12 false positives
```

Both numbers, always together. A gate that refuses everything scores perfectly on the first and is
useless, so the block rate is never shown without the false-positive rate beside it. The benign
corpus also counts escalations separately: asking the principal about a constraint the kernel
cannot decide is the designed behaviour, not an error.

### The dispute defence rate

```
dispute defence: 8/10 defensible with evidence, 0/10 without, improvement 80.0%
  REFUND RECOMMENDED revoked-after-capture (score 0)
  REFUND RECOMMENDED no-evidence-retained (score 0)
```

The two refund recommendations are the point of the exercise. A responder that recommends contesting
everything is worthless, so the cases where the merchant holds evidence and the responder still says
refund are printed by name.

### The scenario suite

```
s01  Purchase lifecycle                           PASS  12/12
s02  Credential attacks                           PASS  14/14
s03  Budget under concurrency                     PASS   5/5
s04  Inventory contention                         PASS   6/6
s05  Structuring and velocity                     PASS   7/7
s06  Revocation races                             PASS   6/6
s07  Escalation and the model boundary            PASS   9/9
s08  Idempotency and webhooks                     PASS   9/9
s09  Evidence and disputes                        PASS   8/8
s10  The degraded path                            PASS   8/8
s11  Soak: mixed traffic                          PASS   6/6

90/90 cases passed across 11 suites in 76.0s
```

Every case declares what it proves and what it expects **before** it runs, and both go into the
report, so a case that quietly stopped proving anything is visible rather than merely green.
Failures are printed and written to the artifact every time.

### The soak

```
9 passed, 231 deselected in 61.95s

25.65s  a batch of purchases and attacks settles correctly
15.22s  a batch of disputes is scored and not all contested
 4.03s  the chain holds over a long run and verifies offline
 2.91s  the ledger still adds up after thousands of reservations
 2.28s  a cap holds against hundreds of simultaneous draws
```

### Offline evidence verification

```
packets read        : 53
signatures verified : 53
chain valid         : True
```

Run with the application stopped, by a script that imports nothing from `app/`. If it needed the
running merchant to agree with it, it would prove nothing about what a third party could check.

## What each suite proves, and why that is valid

A test is only worth its claim if you can say what would fail without it. This is that list.

### s01, Purchase lifecycle

**Proves** an ordinary human-not-present purchase completes, and produces every artifact it is
supposed to.

**Valid because** it checks the artifact at each step, not merely the final status: a discovery
document naming the flow and all four `vct` values, items carrying machine-readable purchase
constraints, signed and hash-addressed policy terms, a merchant-signed Checkout whose policy hash
is the live one, four credentials each validated against the published AP2 schemas before being
sent, a verdict, a capture, a conformant receipt, and an evidence packet. A merchant that returned
`completed` without signing the Checkout would pass a status check and fail here.

**Would fail if** the merchant stopped committing to a price before being asked to sell, or issued
credentials the specification would reject.

### s02, Credential attacks

**Proves** forgery, theft, replay, expiry, exaggerated clock skew and untrusted issuance are each
refused, through the real HTTP endpoint.

**Valid because** every case asserts the **exact** reason code, not merely that something was
refused. Refusing for the wrong reason is a defect that a pass-or-fail check hides, and it matters:
an agent decides what to do next from that code.

**Would fail if** the skew tolerance grew, if the subject binding stopped being checked, or if the
nonce store were dropped. It also pins the tolerance from both sides: 30 seconds ahead is accepted,
3600 is not.

### s03, Budget under concurrency

**Proves** concurrent authorisations cannot all succeed if together they exceed one mandate's cap.

**Valid because** the carts are quoted one at a time and then committed **simultaneously**, which is
the actual contention: several valid, held, priced carts all trying to commit against one cap at the
same moment. Racing the quote instead would test the hold quota.

**Would fail if** the row lock were dropped, or if the value read under it were stale. It did fail,
and that is how the worst defect in this repository was found.

### s04, Inventory contention

**Proves** the merchant cannot sell stock it does not have, one agent cannot exhaust a shelf with
holds it never converts, and a loser is told something it can act on.

**Valid because** it asserts a loser never sees a 500. An agent that cannot tell "sold out" from
"the merchant is broken" retries the wrong thing, so the shape of the refusal is the guarantee, not
just the count of winners.

### s05, Structuring and velocity

**Proves** splitting a purchase does not defeat a budget, a mandate that authorises N uses stops at
N+1, and the merchant's own per-agent limits take effect immediately.

**Valid because** it presents the **same** standing authority repeatedly against fresh quotes, which
is what an agent evading a cap would actually do. Issuing a new mandate each time would reset every
per-mandate counter and prove nothing.

**Would fail if** the aggregates were per-transaction rather than rolling.

### s06, Revocation races

**Proves** a withdrawn mandate stops working at its next use, the revocation is visible with its
reason, and revoking one authority does not touch another.

**Valid because** it reads the gateway mode from `/health` and asserts what that configuration can
actually produce. A gateway that captures inline leaves no window between authorisation and capture
for a revocation to land in; one that returns an unpaid order does. The report says which variant
ran rather than showing a green that tested nothing. The post-capture compensating refund is covered
in-process by `tests/test_money_paths.py::test_revocation_after_capture_compensates_automatically`.

### s07, Escalation and the model boundary

**Proves** no path through a natural-language constraint ends in a sale on its own.

**Valid because** it includes the case that is easy to get wrong: a **clean** cart under a prose
constraint, which the model finds no violation in, still does not complete. That is the deliberate
cost of the model having no approval outcome, and a system that quietly let it through would pass
every other case here.

**Would fail if** `no_violation_found` were ever wired to an approval.

### s08, Idempotency and webhooks

**Proves** a retry never produces a second charge, an unsigned or mis-signed or tampered
notification is refused before parsing, a duplicate capture is harmless, and a failure arriving
after a capture does not unwind real money.

**Valid because** the tampered-body case edits a byte **after** computing the signature, which is
what an attacker would do and what a naive re-serialising verifier would accept.

### s09, Evidence and disputes

**Proves** the chain grows and verifies, a packet reconstructs what the buyer was shown, refusals
are filed as well as sales, and the responder both defends and declines to defend.

**Valid because** it asserts the weak case produces a **refund** recommendation with a non-empty
weaknesses list. A responder that contests everything would pass a "does it produce a
representment" check and lose money in production.

### s10, The degraded path

**Proves** an unverifiable agent can browse, search and quote, and that its refusal is actionable.

**Valid because** it checks the refusal carries an action from the closed set and a retryable flag,
and that the merchant publishes every reason code an agent might see, in advance.

### s11, Soak: mixed traffic

**Proves** all of the above still holds with many agents shopping and attacking at once, over time.

**Valid because** it counts three buckets, not two. A benign purchase refused because the shelf was
empty is not a false positive: the merchant could not have served it. Supply-limited refusals are
counted separately and the shelves are restocked periodically, so the run measures the gate rather
than the warehouse.

**Would fail if** the evidence chain forked under concurrent appends, a budget drifted, or a verdict
appeared without a reason code. Two of those did fail, and both are fixed.

## How to replicate every number

```bash
docker compose up -d
cd backend
python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
cp .env.example .env      # then fill it in, see "Obtaining credentials"
```

```bash
# 1. the fast suite, and the linters CI runs
ruff check app tests
ruff check tools interop scenarios
pytest

# 2. the soak, at the size quoted above
pytest -m soak
#    at the bounded size CI runs
SOAK_SCALE=ci pytest -m soak

# 3. both scorecards
python -m app.cli reports

# 4. the offline evidence check, with nothing else running
python -m app.cli --database "${DB_NAME}_reports" export-evidence --out reports/evidence.jsonl
python -m app.cli export-jwks --out reports/merchant_jwks.json
python tools/verify_evidence.py \
  --jsonl reports/evidence.jsonl \
  --jwks reports/merchant_jwks.json \
  --min-packets 40
```

The scenario suite and the interop driver need a running merchant. Start it for testing, which
selects the recording WhatsApp transport and the stub gateway:

```bash
APP_ENV=testing uvicorn main:app --port 8000
```

```bash
python interop/run_interop.py --base http://127.0.0.1:8000
python scenarios/run_suite.py --base http://127.0.0.1:8000 --profile standard
```

The suite **refuses to run** against a merchant that would send real WhatsApp messages. It reads
the channel modes from `/health` and stops with an explanation rather than driving hundreds of
purchases into somebody's phone. Pass `--allow-live-whatsapp` only if you genuinely want that.

```
$ curl -s localhost:8000/health
{"status":"ok","merchant":"dwarpal-demo-merchant","environment":"testing",
 "whatsapp":"recording","gateway":"stub"}
```

## Filling the dashboard with data

One command, against a running backend:

```bash
cd backend && python scenarios/run_suite.py --profile demo
```

It restocks the catalog, runs all eleven suites at demo scale, and leaves real data behind. From
the run recorded above:

| What appears | Count |
|---|---|
| Policy verdicts in the last 24 hours | 1843 |
| approved / refused / escalated | 1075 / 720 / 48 |
| Agents seen | 1177 |
| Open mandates | 1171 |
| Evidence packets, chain valid | 2079, `true` |
| Captured / refunded | INR 1,243,290.00 / INR 1,950.00 |
| Payment exceptions filed | 3 |
| Disputes, on both sides of contest-or-refund | yes |

Every page then has something on it: the verdict log spans the whole reason-code set, agent traffic
shows budgets partly consumed, mandates show revocations, the evidence browser shows a valid chain
of a couple of thousand packets, and the dispute workspace shows both recommendations.

| Profile | Wall time | Use |
|---|---|---|
| `smoke` | ~10s | Is this merchant configured correctly |
| `standard` | ~50s | What CI runs on every push |
| `demo` | ~80s | Fill the dashboard |
| `soak --minutes 20` | as asked | Sustained mixed traffic |

Useful flags: `--suite s03 s06` to run only those, `--agents 24` to raise concurrency, `--out` to
write the report elsewhere. Exit status is 0 only when every case passed.

### Catalog imagery

Every one of the twelve seeded items carries a photograph. The files live in
`frontend/public/catalog/<sku>.jpg` and the seed in `backend/config/catalog_seed.json` points at
them by path.

They are vendored rather than hotlinked. The images came from Wikimedia Commons, and serving them
straight from `upload.wikimedia.org` through the Next image optimizer was measured at **64 of 122
requests succeeding**, the rest returning HTTP 429. Wikimedia throttles exactly this pattern, and
it is right to: a shop hotlinking a reference library is not a use it owes bandwidth to. Half the
grid rendering as a placeholder is not an acceptable resting state, so the twelve files were
downloaded once, at 1358 KB in total, and are served from the app's own origin. The same check
after vendoring is **120 of 120**.

Provenance is not discarded in the process. `backend/config/catalog_image_credits.json` records,
per SKU, the upstream file name, its Commons page, its licence, the original URL and the local
path, so every picture can be traced back to its source and its terms.

Two things keep this from rotting:

- `backend/tests/test_catalog_seed.py` fails if a seeded item has no image, no alt text, an image
  path that does not resolve to a file on disk, a file small enough to be a failed download, or an
  image with no recorded source and licence. Adding a thirteenth product without a picture turns
  the suite red rather than quietly shipping a gap.
- `frontend/components/product-image.tsx` renders a designed placeholder, a category glyph and tint
  over the item's initials, whenever an image is absent or fails to load in the browser. It is
  reached by nothing in the seeded catalog today, and it exists so that the failure mode is a
  deliberate-looking tile rather than an empty box.

The image is also part of the evidence record. `CatalogEntry.snapshot()` writes `image_url` into
the frozen catalog snapshot, and the evidence browser renders it, because reconstructing what the
buyer was shown at quote time means the picture as well as the price.

## Defects these tests found

Listed because "what broke at 2 AM" is a thing this project is judged on, and because each one was
invisible to the layer above it.

| Defect | Found by | Why the layer above missed it |
|---|---|---|
| **The budget cap could be breached under concurrency.** The row lock was correct; the value read under it came from SQLAlchemy's identity map, so a checkout decided against a balance from before every other session's spend. Five concurrent draws against a two-unit cap all settled. | `s03`, over HTTP | The concurrency fuzz reserved from a fresh session every time. A real checkout has already loaded the mandate before it reaches the kernel, and that is precisely what triggers the stale read. |
| **The evidence chain forked under concurrent appends.** Two checkouts read the same head, computed the same sequence number, and one lost on the primary key with a 500, filing no packet. 25 occurrences in a 20-second run. | `s11`, mixed traffic | In-process tests append one at a time. |
| **A mandate presented by two sessions at once caused a 500.** `_upsert_open_mandate` was a read-then-insert against a unique index. | `s11` | The same. |
| **Publishing a key lost it.** Concurrent agents each read the same JWKS file and wrote back only their own, so all but the last vanished and their genuine credentials were refused as unsigned. | `s11` | One agent at a time never races. |
| **A WhatsApp outage could stall the merchant.** The receipt was sent inside the checkout transaction, so an agent waited on Meta's latency while holding the rows the next checkout needed. A thread-per-receipt fix was worse: it emptied the connection pool. | `s11`, twice | Neither is visible without sustained load and a slow channel. |
| **A rate-limited planner killed the whole buyer run.** Gemini's free tier is twenty requests a day; the twenty-first run ended as an error with no cart, no verdict and no evidence. | driving the console by hand | The tests use the deterministic planner, which never fails. |
| **The dashboard took 28 seconds to render.** One extra query per mandate and per agent. Invisible at ten rows, fatal at seven hundred. | the `demo` profile, then a stopwatch | Nothing before it had ever created seven hundred agents. |
| **A developer's `.env` disabled the test profile.** `APP_ENV=development` in a local `.env` beat the suite's own default, so the tests tried to reach Gemini locally while passing in CI. | running the suite on a machine with a real `.env` | CI sets the variable explicitly, so CI never saw it. |

Each has a regression test. The budget one has a control that fails against the old code: thirty
draws granted where ten were allowed.

---

# Standards

Implemented against the AP2 specification at https://ap2-protocol.org/. The reference
implementation is at https://github.com/google-agentic-commerce/AP2.

The JSON Schemas under `backend/app/ap2/schemas/` are copied verbatim from that repository at
commit `e1ea56db72a6385bce3e5c1112b3a56ce60acb43` (2026-04-29) and redistributed under its Apache
2.0 licence. Every credential Dwarpal issues and every credential it accepts is validated against
them at run time, so conformance is checked by the reference implementation's own definitions
rather than asserted here.

Dwarpal is designed for NPCI's Unified Agent Protocol, which is in development and not yet
published. It is not UAP compliant and does not claim to be.

## Vocabulary

The current specification uses two credential families with two stages each. The
"Intent Mandate, Cart Mandate, Payment Mandate" triad is the September 2025 launch language and is
out of date; it appears nowhere in this repository.

| Credential | `vct` | Signed by |
|---|---|---|
| Open Checkout Mandate | `mandate.checkout.open.1` | the human, via a trusted surface |
| Closed Checkout Mandate | `mandate.checkout.1` | the agent |
| Open Payment Mandate | `mandate.payment.open.1` | the human, via a trusted surface |
| Closed Payment Mandate | `mandate.payment.1` | the agent |
| Checkout | merchant record, embedded as `checkout_jwt` | the merchant |

## Conformance matrix

| Area | Status | Evidence |
|---|---|---|
| Human-not-present flow | Implemented | `interop/driver.py` drives it end to end; `tests/test_checkout_flow.py`; `scenarios/suites/s01` |
| Human-present flow | Not implemented | Out of scope; the merchant's verification duty is not the interesting case there |
| Merchant role | Implemented | Quote, merchant-signed Checkout, verification, checkout receipt |
| Merchant Payment Processor role | Implemented | Order creation, capture, refunds, reconciliation against Razorpay |
| Credential Provider role | Mocked | `app/harness/factory.py`. Out of scope, as section 3 of the specification allows |
| Trusted Surface | Mocked | Same module. It signs the open mandates the way a trusted surface would |
| Open Checkout Mandate | Implemented | Verified, and validated against the published schema |
| Closed Checkout Mandate | Implemented | Verified, including `checkout_hash` binding to the merchant-signed Checkout |
| Open Payment Mandate | Implemented | All eight published constraint types evaluated deterministically |
| Closed Payment Mandate | Implemented | `transaction_id` binding and amount agreement enforced |
| Checkout Receipt | Implemented | Signed by the merchant, validated against the published schema |
| Payment Receipt | Not implemented | Dwarpal is not the network; the MPP receipt it would sign is not produced |
| SD-JWT selective disclosure | Implemented | `app/ap2/sdjwt.py`, RFC 9901 disclosures and digests |
| SD-JWT key binding | Implemented | KB-JWT with `sd_hash`, `aud` and `nonce`, verified against `cnf.jwk` |
| SD-JWT delegation chains | Not implemented | Single-hop issuance only; multi-hop agent delegation is not supported |
| x402 flow | Not implemented | Card flow only |
| A2A transport | Not implemented | Dwarpal speaks HTTP and MCP, not the A2A envelope |
| Schema validation | Implemented | Vendored schemas, enforced on issue and on accept |

`SPEC_DELTA.md` is the companion to this table: every place the built system differs from
`IMPLEMENTATION_PLAN.md`, with a judgement about which is better and why.

## What the interop claim rests on

Two halves, and they are not equally strong.

**Machine-verified here.** `backend/interop/run_interop.py` plays the Trusted Surface, the
Shopping Agent and the mocked Credential Provider against a running Dwarpal over HTTP. It drives a
complete human-not-present purchase, the degraded path for an unverified agent, an attack that is
refused with a reason code, and revocation after capture. Every credential it puts on the wire is
validated against the published AP2 JSON Schemas before it is sent, so the run cannot pass by
feeding Dwarpal something the specification would reject. This runs in CI on every push, alongside
the scenario suite.

**Not verified here.** The upstream reference shopping agent (`shopping_agent_v2` in the AP2
repository) is a Google ADK application that needs `uv` and a Google API key, and it speaks A2A
rather than plain HTTP. Dwarpal exposes an MCP server at `app/mcp/server.py` that presents the
catalog, the policy terms and the quote endpoint in the shape that agent expects, but the two have
not been run against each other in this repository, and the matrix says so rather than implying
otherwise.

## Verified against the live services

The automated suite stubs Razorpay, Gemini and Meta, as it must. Separately, these paths were
exercised against the real services in test mode, and the results are recorded here rather than
implied.

### Razorpay, fully verified

A real card payment was driven through Razorpay test-mode Checkout against an order Dwarpal
created, and the whole loop closed:

| Step | Evidence |
|---|---|
| Order created by Dwarpal | `order_TT6Kw1Rn1YtU43`, notes round-tripped |
| Card authorised | `pay_TT6Q2UcBMTAePY`, status `authorized` |
| `payment.authorized` webhook | delivered by `Razorpay-Webhook/v1`, signature verified, HTTP 200 |
| Capture by Dwarpal's own code | real API call, payment moved to `captured` |
| `payment.captured` webhook | delivered and verified; it finalised the Dwarpal checkout |
| Checkout settled | budget committed, spend recorded, stock consumed, state `completed` |
| Full compensating refund | `rfnd_TT6Ty0N8V9AH14` for the full amount, payment `refunded` |
| `refund.created` / `refund.processed` | delivered and verified |

Nine webhooks arrived from Razorpay across the run. Every one carried a genuine
`X-Razorpay-Signature` that the HMAC check accepted, and every one answered HTTP 200. Unsigned and
mis-signed bodies were refused 401 before parsing.

The buyer console's order creation was re-verified during this change: a run through
`/buyer` produced `order_TTnNQ3xgubvtQk` against the live test-mode API, reached
`awaiting_payment` with an approving verdict and an evidence packet, and rendered the
**Pay with the test card** button. Entering the card itself is left to you.

### WhatsApp, verified end to end

Driven against the live Meta Cloud API on 2026-08-25, against phone number `+91 80859 28343`
("Samvaad", quality GREEN) and recipient `+91 70674 66990`.

| Path | State | Evidence |
|---|---|---|
| Access token and phone number id | verified | System User token, scopes `whatsapp_business_messaging`, `whatsapp_business_management` |
| Webhook verification handshake | verified | correct verify token answered 200 with the challenge echoed; a wrong one answered 403 |
| App webhook subscription | verified | `whatsapp_business_account` object, active, `messages` field subscribed, callback pointing at the tunnel |
| Outbound escalation, free-form | verified | delivered with Approve and Deny buttons, `wamid.HBgMOTE3...` |
| Outbound escalation, template | **fails, and is handled** | the configured template does not exist; see below |
| Outbound purchase receipt, completed | verified | sent, routed to the connection's number |
| Outbound purchase receipt, refused | verified | sent, naming `ITEM_AGE_RESTRICTED` |
| Outbound purchase receipt, compensated | verified | sent |
| Inbound delivery statuses | verified | Meta posted `sent` then `delivered` to the webhook, both passing the HMAC check |
| Inbound button reply | verified with a real tap | Approve tapped on a phone reached the webhook as `facebookexternalua`, signed, and settled the escalation |
| Answered once | verified with real traffic | a second real tap on an already-settled escalation was recorded and ignored as `already_answered` |

### The two templates Dwarpal needs

Dwarpal sends exactly two kinds of message, so it needs exactly two approved templates. Both are
UTILITY. Neither exists on this account today: WhatsApp Business Account `2417951275365258`, the
one the sending number belongs to, holds only `hello_world` and
`3p_direct_integration_test_template`.

Templates are per account. A template approved on a different WhatsApp Business Account cannot be
used by a number that belongs to this one, which is the most likely reason a template you believe
is approved answers `132001 template name does not exist`.

**1. Escalation, the one with the buttons.** Raised when the kernel cannot settle a constraint on
its own and has to ask the human. Five body parameters and two quick-reply buttons:

```
{{1}} is asking you to confirm an automated purchase.

Amount: {{2}}
Cart: {{3}}

This could not be decided automatically because of your instruction:
"{{4}}"

Reference: {{5}}
```

| Parameter | Carries | Example |
|---|---|---|
| `{{1}}` | merchant name | Dwarpal Demo Store |
| `{{2}}` | amount | INR 1450.00 |
| `{{3}}` | cart summary | 2 x Nilgiri Black Tea 250g |
| `{{4}}` | the constraint that could not be decided | nothing perishable |
| `{{5}}` | escalation reference | 532d0fdfdc4f4600807a10123933037e |

Buttons: two quick replies, **Approve** first and **Deny** second. The order matters. Dwarpal sets
each button's payload per send, at index 0 and index 1, and that payload is what carries the
escalation id back. Set `META_TEMPLATE_NAME` to the template name and `META_TEMPLATE_LANGUAGE` to
the exact locale it was published in, which is usually `en_US` rather than `en`.

**2. Purchase receipt, no buttons.** One template covers all three outcomes: completed, refused,
and reversed after a revocation. The fourth parameter is what differs between them, so there is no
need for three templates.

```
An agent acted on your behalf at {{1}}.

Amount: {{2}}
Items: {{3}}
Outcome: {{4}}

Reference: {{5}}
```

| Parameter | Carries | Example |
|---|---|---|
| `{{1}}` | merchant name | Dwarpal Demo Store |
| `{{2}}` | amount | INR 1450.00 |
| `{{3}}` | items | 2 x Nilgiri Black Tea 250g |
| `{{4}}` | what happened | The purchase completed. / The purchase was refused: BUDGET_EXCEEDED. |
| `{{5}}` | correlation reference | dwc_ae823f85046d4a8bb527b296f4ddca0a |

Set `META_RECEIPT_TEMPLATE_NAME` and `META_RECEIPT_TEMPLATE_LANGUAGE` once it is approved.

**What happens with neither.** Both paths fall back to a free-form message, which is what runs
today and what was verified in this session. Meta only delivers free-form messages inside the 24
hour window after the person last messaged the business number, so an escalation raised into a
quiet inbox would not arrive at all. It would sit pending and become a denial at its deadline,
because the timeout fails closed. That is safe, and it is not useful.

The escalation service tries the template first, records any failure in the escalation's
`delivery_error`, and then falls back, so a template that is misconfigured or still awaiting review
never silences the question. That was confirmed live: the template send failed with `132001`, the
error was recorded, and the free-form message delivered.

### This WhatsApp Business Account is shared with two other apps

Three applications are subscribed to WABA `2417951275365258`: `dwarpal`, `Socialyfy` and
`SAMVAAD-Dev`. A webhook subscription is made per account, not per app or per number, so **all
three receive every event for both numbers on the account**.

That is the explanation for a reply arriving from a number Dwarpal never sends from. On
2026-08-25 a button was tapped on the Samvaad number at 06:07:19 and 06:07:21; five seconds later
two messages went out from the Test Business number, `+91 93034 56309`. Dwarpal posts only to
`META_PHONE_NUMBER_ID` and never replies to a tap at all, so those came from one of the other two
apps, which received the same tap and answered it from its own number.

Dwarpal now defends its own side of this. `parse_inbound` reads only events whose
`metadata.phone_number_id` matches the configured number, and the webhook reports what it ignored:

```
POST /webhooks/whatsapp   (a reply on the other number)
{"received": true, "applied": [], "ignored_other_numbers": ["1265280240001529"]}
```

So somebody else's reply can never settle an escalation here. What it cannot fix is the other
direction: those apps still see this merchant's traffic and may answer it. To stop that, unsubscribe
them from the account in Meta's app settings, or give Dwarpal a WhatsApp Business Account of its
own. `python -m app.cli check-channels` reports the situation whenever it holds.

**A note on the verify token.** The configured one contains a `#`. That is fine for Meta, which
URL-encodes it properly, but it truncates any hand-written `curl` command that puts it in a query
string unencoded. If you test the handshake by hand, encode it.

### Checking the channels without sending anything

The template defect above was invisible until an escalation needed it, because Meta accepts the
send and answers with an error only for that one message, which the free-form fallback then
quietly covers for. This is the command that asks the question directly:

```bash
cd backend && python -m app.cli check-channels
```

It reads and never sends: the Razorpay key mode, the Meta token and its scopes, the phone number
and its quality rating, whether the webhook Meta has on file still points at this merchant, and
whether every template named in configuration actually exists in the language it is named with.
Exit status is 0 only when all of them pass.

Set `META_WABA_ID` for the template check to work. It is the WhatsApp Business Account id, and it
appears as `entry[0].id` in any inbound webhook payload. Without it the check fails rather than
skipping, because a check that quietly skips the thing it was written for is worse than no check.

Its current output on this account:

```
  [ok  ] razorpay key is test mode
  [ok  ] escalation recipient is E.164          +917067466990
  [ok  ] whatsapp credentials present
  [ok  ] access token is valid                  SYSTEM_USER, whatsapp_business_management,
                                                whatsapp_business_messaging
  [ok  ] phone number is reachable              +91 80859 28343 (Samvaad), quality GREEN
  [ok  ] webhook points back here
  [FAIL] template META_TEMPLATE_NAME            dwarpal_purchase_approval does not exist at all
```

---

# Running it

## Repository layout

```
backend/            FastAPI application
  app/              the merchant: kernel, verification, checkout, evidence, disputes
  scenarios/        the HTTP scenario suite, and the dashboard data generator
  interop/          the AP2 interop driver
  tests/            the fast suite and the soak
  tools/            the standalone evidence verifier, which imports nothing from app/
frontend/           Next.js: the landing page, the login, and the two consoles
scripts/            tunnel helper for the webhook callbacks
SPEC_DELTA.md       where the code differs from the plan, and which is right
```

## Prerequisites

- Python 3.12 or newer
- Node.js 20 or newer
- Docker and Docker Compose, for PostgreSQL
- ngrok, only if you want Razorpay and Meta to reach your webhooks

## Setup

Start the database:

```bash
docker compose up -d
```

If port 5432 is already taken on your machine, set `DB_PORT` in a root `.env` (compose reads it)
and in `backend/.env`, so the container publishes on a free port.

Backend:

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

Frontend:

```bash
cd frontend
npm install
cp .env.example .env.local
```

Fill in the values in `backend/.env` before starting the backend. It validates its configuration
at startup and will refuse to run if a required value is missing.

## Obtaining credentials

**Razorpay.** Sign in at https://dashboard.razorpay.com/ and switch the dashboard to **Test Mode**
before doing anything else. Generate a key pair under Account and Settings, API Keys. The key
identifier starts with `rzp_test_`; Dwarpal refuses to start with a live key, because a defect on
the checkout path would otherwise move real money. The secret is shown once and cannot be
retrieved later.

Then create the webhook, still in Test Mode, under Account and Settings, Webhooks, Add New
Webhook:

- **Webhook URL**: your public origin followed by `/webhooks/razorpay`
- **Secret**: any value you choose. Put the identical value in `RAZORPAY_WEBHOOK_SECRET`. It is
  optional in the form but Dwarpal rejects unsigned webhooks before parsing them.
- **Alert Email**: your own address
- **Active Events**: exactly these seven, and nothing else:
  `payment.authorized`, `payment.captured`, `payment.failed`, `order.paid`, `refund.created`,
  `refund.processed`, `refund.failed`

  The three refund events are not decoration. The revocation-after-capture path depends on seeing
  the compensating refund reach a terminal state.
- When the dashboard prompts for an OTP in test mode, the documented test OTP is `754081`.

Test-mode and live-mode webhooks are configured separately. A webhook created in live mode will
never fire for a test payment.

**Gemini.** Create an API key at https://aistudio.google.com/apikey. The free tier allows twenty
`gemini-2.5-flash` requests a day; past that, the buyer console falls back to its deterministic
planner and the merchant's semantic check escalates to the human. Both are the designed behaviour,
and both say so in their logs.

**WhatsApp.** Create an app at https://developers.facebook.com/ and add the WhatsApp product.

- WhatsApp, API Setup: the access token and the phone number identifier. Add your own number as a
  verified test recipient, or sends are rejected. The token offered on that page expires in 24
  hours; for anything longer, create a System User under Business Settings with the
  `whatsapp_business_messaging` and `whatsapp_business_management` permissions and generate a
  permanent token.
- App Settings, Basic: the app secret and the app id.
- WhatsApp, Configuration, Webhooks, Edit: the callback URL is your public origin followed by
  `/webhooks/whatsapp`, and the verify token is a value you choose and also put in
  `META_VERIFY_TOKEN`. The backend must be running and reachable when you press Verify and Save,
  because Meta immediately issues a GET that Dwarpal has to answer. Subscribe to the `messages`
  field only.

**Application secret.** Any high-entropy random string, at least 16 characters. Thirty-two or more
is preferable.

**Merchant signing keys.** Generated automatically on first run into the directory named by
`MERCHANT_SIGNING_KEY_DIR`. Do not commit them. In a deployed environment, mount that directory as
persistent storage so previously signed records stay verifiable across restarts.

## Public callbacks

Razorpay and Meta both have to reach your machine. From the repository root:

```powershell
./scripts/tunnel.ps1
```

It prints the public origin, the two callback URLs, and the `PUBLIC_BASE_URL` line to paste into
`backend/.env`. That value matters beyond the tunnel: it is the audience an agent must put in its
key-binding proof, and the merchant publishes it in the discovery document.

## Running

Backend:

```bash
cd backend
uvicorn main:app --reload
```

Frontend:

```bash
cd frontend
npm run dev
```

The site runs at http://localhost:3000 and proxies API calls to the origin named by
`BACKEND_ORIGIN`. Only the merchant and buyer-console surfaces are proxied; the agent endpoints are
not, because agents transact against the backend directly and must not reach it through the
dashboard's origin.

## Driving a purchase from a terminal

One command, against a running backend:

```bash
cd backend
python interop/run_interop.py
```

It plays the Trusted Surface, the Shopping Agent and the mocked Credential Provider, and reports
every check it made. Watch the result appear in the merchant verdict log and evidence browser.

### Pointing Claude at the catalog over MCP

The catalog is also an MCP server, exposing `merchant_profile`, `browse_catalog`, `search_catalog`,
`get_item`, `list_categories`, `get_policy_terms` and `quote_cart`. It is read-and-quote only:
completing a purchase goes through the HTTP endpoint, where the full verification pipeline runs.

From a shell with the virtual environment activated:

```bash
cd backend
python -m app.mcp.server            # stdio
python -m app.mcp.server --http     # streamable HTTP on 127.0.0.1:8765/mcp
```

An MCP client is different. It spawns the command itself, with your system PATH, and never
activates a virtual environment, so `"command": "python"` finds an interpreter that does not have
the dependencies and fails with `ModuleNotFoundError: No module named 'mcp'`. Give it the
interpreter by absolute path, and set `cwd` to `backend` so the `app` package can be imported:

```json
{
  "mcpServers": {
    "dwarpal": {
      "command": "C:/absolute/path/to/dwarpal/backend/.venv/Scripts/python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "C:/absolute/path/to/dwarpal/backend"
    }
  }
}
```

On macOS or Linux the interpreter is `backend/.venv/bin/python`.

PostgreSQL must be running, because the MCP server reads the database directly rather than going
through the HTTP backend. `docker compose up -d` is enough on its own; uvicorn does not have to be
up. Configuration is read from `backend/.env` by absolute path, so only `cwd` matters for imports.

Set `MCP_PUBLIC_URL` in `backend/.env` to the address you serve MCP on, and the discovery document
advertises it under `endpoints.mcp`. Left unset, discovery omits the endpoint rather than
publishing an address that answers 404.

## Checks

```bash
cd backend && ruff check app tests && ruff check tools interop scenarios && pytest
```

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run build
```

CI runs the same checks on every push and pull request to `main`, plus the soak at CI scale, the
report generation, the offline evidence verification, the interop driver and the scenario suite
against a live merchant, and a container build for both images.

## Verifying the evidence offline

The Evidence Locker is append-only, enforced by a PostgreSQL trigger rather than by application
convention, and hash chained so any retroactive edit is detectable. Appends are serialised by a
database advisory lock, because a hash chain is serial by definition and two writers choosing the
same predecessor is the chain forking, not a contention problem to tune away.

The verifier is a standalone script that imports nothing from the application:

```bash
cd backend
python -m app.cli export-evidence --out reports/evidence.jsonl
python -m app.cli export-jwks --out reports/merchant_jwks.json

# Stop the application, then:
python tools/verify_evidence.py --jsonl reports/evidence.jsonl --jwks reports/merchant_jwks.json
```

It can also read the database directly with `--dsn`. Exit status is 0 only when every hash link
and every signature checks out, and when at least `--min-packets` packets were read. That floor
defaults to 1, because an empty chain satisfies every link vacuously: without it a verifier pointed
at the wrong database reports success while checking nothing. Pass `--min-packets 0` to accept an
empty chain deliberately.

`export-evidence` reads the live database. The two report runs write to their own databases so
neither truncates the other's chain, so exporting the corpus chain means naming it:

```bash
python -m app.cli --database "${DB_NAME}_reports" export-evidence --out reports/evidence.jsonl
```

This is what CI does, so the offline check there runs against a real chain of about fifty packets
rather than an empty one.
