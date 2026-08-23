# Dwarpal frontend

The merchant dashboard. This is the human's view of agent traffic. Buyer agents never touch it;
they transact against the backend directly.

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
| `/` | Approvals, refusals, escalations and challenges over the last day, plus the latest decisions |
| `/traffic` | Which agents are transacting, under whose authority, and their spend against remaining budget |
| `/verdicts` | Every policy decision with its reason code, filterable, refusals as prominent as approvals |
| `/mandates` | Open mandates in force, their constraints and consumption, with revocation |
| `/agents` | Per-agent window limits, category gates and the kill switch |
| `/evidence` | The packet chain and its verification status |
| `/evidence/[correlationId]` | One packet rendered readably: authority, catalog snapshot, verdicts, timings |
| `/disputes` | The dispute workspace |
| `/disputes/[disputeId]` | The representment and the contest-or-refund recommendation, with its scoring |
| `/scorecards` | The attack and dispute numbers rendered from the generated reports |

## Setup

```bash
npm install
cp .env.example .env.local
```

`BACKEND_ORIGIN` names the backend the dashboard proxies to, and is the only setting the app
reads.

## How it talks to the backend

The URL exists in exactly one place, `lib/backend.ts`, which is `server-only`. Server Components
read through it and Server Actions write through it. Client components that need to poll go
through the same-origin catch-all at `app/api/dwarpal/[...path]/route.ts`, which forwards only the
merchant surface. The browser never makes a cross-origin request, so there is no CORS
configuration anywhere.

## Running

```bash
npm run dev
```

Runs at http://localhost:3000. The backend must be running separately. If it is not, every page
says so and tells you how to start it rather than rendering an empty shell.

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

Route handler params are typed explicitly rather than with the generated `RouteContext` helper,
because that helper only exists once a build has emitted `.next/types` and CI type-checks before
it builds.
