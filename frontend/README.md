# Dwarpal frontend

The public page, the view chooser, and the two consoles. Buyer agents never touch any of it; they
transact against the backend directly.

## Stack

| Layer | Technology |
|---|---|
| Framework | Next.js 16, App Router |
| Language | TypeScript |
| Styling | Tailwind CSS v4 |
| Lint | ESLint 9, flat config |

## Surfaces

| Route | What it shows |
|---|---|
| `/` | The public page: what the project is, the walkthrough video, the live scorecard numbers, and what is deliberately not built |
| `/login` | Two doors, no password. Sets a cookie deciding which console you get |
| `/merchant` | Approvals, refusals, escalations and challenges over the last day, plus the latest decisions |
| `/merchant/traffic` | Which agents are transacting, under whose authority, and their spend against remaining budget |
| `/merchant/verdicts` | Every policy decision with its reason code, filterable, refusals as prominent as approvals |
| `/merchant/mandates` | Open mandates in force, their constraints and consumption, with revocation |
| `/merchant/agents` | Per-agent window limits, category gates and the kill switch |
| `/merchant/connections` | Mint a connection for somebody else's agent, and the receipts it has been sent |
| `/merchant/evidence` | The packet chain and its verification status |
| `/merchant/evidence/[correlationId]` | One packet rendered readably: authority, catalog snapshot, verdicts, timings |
| `/merchant/disputes` | The dispute workspace |
| `/merchant/disputes/[disputeId]` | The representment and the contest-or-refund recommendation, with its scoring |
| `/merchant/scorecards` | The attack and dispute numbers rendered from the generated reports |
| `/buyer` | Send an agent shopping, with a budget cap and prose constraints |
| `/buyer/runs` | Every run this console has driven |
| `/buyer/runs/[runId]` | One agent's log, live, and the Razorpay test-card payment when it is approved |
| `/buyer/catalog` | What the merchant sells, as an agent reads it |
| `/buyer/setup` | How to point your own agent at this merchant |

## Design

One light theme, chosen deliberately, with no dark variant. The tokens are all in
`app/globals.css`: white paper, navy ink, one blue that means "interactive", and four semantic
colours that mean exactly one thing each and are never used decoratively. Every id, hash and amount
is set in the monospace face, because those are engineered values and should look it.

Three rules the components enforce:

- **Refusals are as prominent as approvals.** A merchant's refusals are the record that defends a
  dispute; hiding them as error states would be the wrong instinct twice over.
- **Tables collapse into labelled cards below `md`** rather than forcing a sideways scroll. Wide
  content that genuinely must stay wide scrolls inside its own `.scroll-x` container; the page body
  never scrolls horizontally at any width.
- **Motion is transform and opacity only**, on one easing curve, and fully disabled under
  `prefers-reduced-motion`.

Navigation is a single surface. `components/sidebar.tsx` is a 264px rail at `lg`, a 72px icon rail
at `md`, and a focus-trapped drawer below that.

## How it talks to the backend

The URL exists in exactly one place, `lib/backend.ts`, which is `server-only`. Server Components
read through it and Server Actions write through it. Client components that need to poll go
through the same-origin catch-all at `app/api/dwarpal/[...path]/route.ts`, which forwards only the
merchant and buyer-console surfaces. The agent endpoints are deliberately not proxied: an agent
transacts against the backend directly and must not be able to reach it through this origin.

The browser never makes a cross-origin request, so there is no CORS configuration anywhere. The one
external script is Razorpay Checkout, loaded only on a run that is waiting to be paid.

## Setup

```bash
npm install
cp .env.example .env.local
```

`BACKEND_ORIGIN` names the backend to proxy to. `MERCHANT_API_TOKEN` must match the backend's, and
is sent server side as `X-Merchant-Token`; it never reaches the browser.

## Running

```bash
npm run dev
```

Runs at http://localhost:3000. The backend must be running separately. If it is not, every console
page says so and tells you how to start it rather than rendering an empty shell.

## Checks

```bash
npm run lint
npx tsc --noEmit
npm run build
```

All three must pass before any change is considered complete.

## Note on Next.js 16

This project is on Next.js 16, which changed APIs and conventions from earlier releases:
`params` and `searchParams` are promises, `middleware.ts` is now `proxy.ts`, and `next lint` no
longer exists so the `lint` script runs `eslint` directly. The authoritative documentation for the
installed version is in `node_modules/next/dist/docs/`.

Route handler and page params are typed explicitly rather than with the generated `RouteContext`
and `PageProps` helpers, because those only exist once a build has emitted `.next/types` and CI
type-checks before it builds.

A `"use server"` module may only export async functions, which is why `app/login/persona.ts` holds
the cookie name and the persona type while `app/login/actions.ts` holds only the action.
