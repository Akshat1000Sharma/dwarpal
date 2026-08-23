# Dwarpal

The AP2 merchant endpoint for Razorpay. Dwarpal makes a Razorpay merchant transactable by an AI
buyer agent, end to end, and keeps the evidence that defends the transaction if it is later
disputed.

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
- Assembles dispute representments, and says when the evidence is too weak to contest.

Agents that cannot present acceptable credentials are not refused outright. They may browse,
quote and build a cart, but cannot check out above a configured ceiling or buy restricted
categories.

## Where the model is used, and where it is not

The deterministic policy kernel makes every money decision. No model is called on that path, and
that is enforced structurally rather than by convention: `tests/test_kernel_isolation.py` walks the
transitive import closure of every module in `app/kernel/` and fails the build if any model client
or network client is reachable from it.

A model is used in exactly one place: evaluating constraints expressed in natural language, which
arithmetic cannot check. Its authority is deliberately clipped. Two separate types carry the
invariant:

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

## Architecture

| Component | Responsibility |
|---|---|
| Catalog and discovery | Agent-readable inventory, purchase constraints, signed policy terms |
| Credential verification | AP2 mandate chain validation and constraint satisfaction |
| Policy kernel | Deterministic, reason-coded, signed verdicts on every money action |
| Semantic check | Natural-language constraint evaluation, deny or escalate only |
| Escalation | Human approve or deny over WhatsApp, with a deadline that fails closed |
| Checkout | Headless quote to fulfilment on Razorpay test mode |
| Evidence Locker | Hash-chained, offline-verifiable transaction records |
| Dispute responder | Representment assembly and contest-or-refund recommendation |
| Dashboard | Merchant view of traffic, verdicts, mandates, evidence and disputes |

## Standards

Implemented against the AP2 specification at https://ap2-protocol.org/. The reference
implementation is at https://github.com/google-agentic-commerce/AP2.

The JSON Schemas under `backend/app/ap2/schemas/` are copied verbatim from that repository at
commit `e1ea56db72a6385bce3e5c1112b3a56ce60acb43` (2026-04-29) and redistributed under its Apache
2.0 licence. Every credential Dwarpal issues and every credential it accepts is validated against
them at run time, so conformance is checked by the reference implementation's own definitions
rather than asserted here.

Dwarpal is designed for NPCI's Unified Agent Protocol, which is in development and not yet
published. It is not UAP compliant and does not claim to be.

### Vocabulary

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
| Human-not-present flow | Implemented | `interop/driver.py` drives it end to end; `tests/test_checkout_flow.py` |
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

### What the interop claim rests on

Two halves, and they are not equally strong.

**Machine-verified here.** `backend/interop/run_interop.py` plays the Trusted Surface, the
Shopping Agent and the mocked Credential Provider against a running Dwarpal over HTTP. It drives a
complete human-not-present purchase, the degraded path for an unverified agent, an attack that is
refused with a reason code, and revocation after capture. Every credential it puts on the wire is
validated against the published AP2 JSON Schemas before it is sent, so the run cannot pass by
feeding Dwarpal something the specification would reject. This runs in CI on every push.

**Not verified here.** The upstream reference shopping agent (`shopping_agent_v2` in the AP2
repository) is a Google ADK application that needs `uv` and a Google API key, and it speaks A2A
rather than plain HTTP. Dwarpal exposes an MCP server at `app/mcp/server.py` that presents the
catalog, the policy terms and the quote endpoint in the shape that agent expects, but the two have
not been run against each other in this repository, and the matrix says so rather than implying
otherwise.

## Repository layout

```
backend/    FastAPI application, tests, adversarial corpus, interop driver
frontend/   Next.js 16 merchant dashboard
scripts/    Tunnel helper for the webhook callbacks
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

**Gemini.** Create an API key at https://aistudio.google.com/apikey.

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

The dashboard runs at http://localhost:3000 and proxies API calls to the backend origin named by
`BACKEND_ORIGIN`. Agents never touch the dashboard; they transact against the backend directly.

## Driving a purchase

One command, against a running backend:

```bash
cd backend
python interop/run_interop.py
```

It plays the Trusted Surface, the Shopping Agent and the mocked Credential Provider, and reports
every check it made. Watch the result appear in the dashboard's verdict log and evidence browser.

The catalog is also available over MCP:

```bash
cd backend
python -m app.mcp.server            # stdio
python -m app.mcp.server --http     # streamable HTTP
```

## Checks

```bash
cd backend && ruff check app tests && pytest
```

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run build
```

CI runs the same checks on every push and pull request to `main`, plus the interop driver against
a live merchant and a container build for both images.

## Reports

Both numbers are produced by running code and written to `backend/reports/`. CI uploads them as
build artifacts.

```bash
cd backend
python -m app.cli reports
```

The attack scorecard reports blocks, misses and the false-positive rate against a matched benign
corpus together, always. A gate that refuses all traffic would score perfectly on the first number
and be useless, so it is never shown alone.

## Verifying the evidence offline

The Evidence Locker is append-only, enforced by a PostgreSQL trigger rather than by application
convention, and hash chained so any retroactive edit is detectable. The verifier is a standalone
script that imports nothing from the application:

```bash
cd backend
python -m app.cli export-evidence --out reports/evidence.jsonl
python -m app.cli export-jwks --out reports/merchant_jwks.json

# Stop the application, then:
python tools/verify_evidence.py --jsonl reports/evidence.jsonl --jwks reports/merchant_jwks.json
```

It can also read the database directly with `--dsn`. Exit status is 0 only when every hash link
and every signature checks out.
