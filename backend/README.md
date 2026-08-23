# Dwarpal backend

FastAPI service implementing the AP2 merchant and merchant payment processor roles against
Razorpay test mode.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI |
| Language | Python 3.12 |
| Database | PostgreSQL |
| Payments | Razorpay (test mode), called over its REST API |
| Model | Gemini via google-genai, semantic constraint check only |
| Messaging | Meta WhatsApp Cloud API |
| Lint | ruff |
| Tests | pytest |

## Responsibilities

- Serve the agent-readable catalog, the discovery document and the merchant-signed policy terms.
- Verify inbound AP2 credentials and the constraint satisfaction the specification requires of the
  merchant.
- Run the deterministic policy kernel that authorises or refuses every money action.
- Execute headless checkout against Razorpay, including compensating refunds.
- Write and serve hash-chained evidence packets.
- Assemble dispute representments.
- Generate the attack scorecard and dispute defence reports.

## Layout

```
app/ap2/          JOSE, SD-JWT, AP2 models, vendored schemas, constraint evaluation
app/kernel/       the deterministic policy kernel. No model client is reachable from here
app/semantic/     the model boundary: deny or escalate, never approve
app/verification/ the seven ordered verification steps
app/checkout/     quote, the completion orchestrator, idempotency
app/payments/     Razorpay gateway, settlement, reconciliation
app/evidence/     the append-only, hash-chained Evidence Locker
app/harness/      the adversarial and benign corpora, and the report generators
app/mcp/          catalog MCP server
interop/          the AP2 interop driver
tools/            the standalone evidence verifier, which imports nothing from app/
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` before starting. Configuration is validated at startup and the application will
refuse to run if a required value is missing or malformed. It also refuses to start against a live
Razorpay key: `RAZORPAY_KEY_ID` must begin `rzp_test_`.

PostgreSQL is expected to be running. From the repository root:

```bash
docker compose up -d
```

## Running

```bash
uvicorn main:app --reload
```

## Checks

```bash
ruff check app tests
pytest
```

Both must pass before any change is considered complete. The suite requires PostgreSQL and will
fail with an instruction rather than skipping: the budget and inventory guarantees are tested
against real row-level locking, and a suite that quietly skipped them would be dishonest.

## Command line

```bash
python -m app.cli reports            # regenerate both reports into reports/
python -m app.cli scorecard          # the attack scorecard only
python -m app.cli disputes           # the dispute defence report only
python -m app.cli export-evidence    # write the evidence chain as JSONL
python -m app.cli export-jwks        # write the merchant public JWK Set
python -m app.cli verify-chain       # in-process chain check
python -m app.cli seed               # create the schema and seed the catalog
```

Report generation runs against a separate `<DB_NAME>_reports` database, so a run never disturbs
live data.

## Interop

```bash
python interop/run_interop.py
python interop/run_interop.py --base https://your-tunnel.ngrok-free.dev
```

Plays the Trusted Surface, the Shopping Agent and the mocked Credential Provider against a running
Dwarpal, and validates every credential against the published AP2 schemas before sending it.

## The adversarial corpus

Scenarios are data under `app/harness/corpus/`, not hand-written test functions, so a new attack
family is a YAML file rather than a code change. Every scenario runs in CI through
`tests/test_corpus.py`, and the same runner produces the scorecard.

The benign corpus runs alongside the adversarial one. Blocks and false positives are reported
together, always.

## Environment

Every setting the application reads is listed in `.env.example`. Instructions for obtaining each
credential are in the root `README.md`.

Merchant signing keys are generated on first run into the directory named by
`MERCHANT_SIGNING_KEY_DIR`. That directory is gitignored and must never be committed. In a
deployed environment, mount it as persistent storage so records signed before a restart remain
verifiable.

## Reports

The attack scorecard and the dispute defence numbers are generated into `reports/`. CI uploads
that directory as a build artifact. Reports state both blocks and misses, and the false-positive
rate against the benign corpus, in every run.
