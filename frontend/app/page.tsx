import Image from "next/image";
import Link from "next/link";

import {
  BrandHeading,
  CLAUDE_ORANGE,
  ClaudeMark,
  WHATSAPP_GREEN,
  WhatsAppMark,
} from "@/components/brand";
import { LandingVideo } from "@/components/landing-video";
import { Reveal } from "@/components/reveal";
import { backendRead } from "@/lib/backend";
import type { Reports } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * The public page.
 *
 * One argument, made once: the merchant carries the loss, so the merchant has to hold the gate.
 * Every section is a step in that argument, the numbers are read from the running merchant rather
 * than written into the copy, and the section that admits what is not built is not an afterthought.
 */
export default async function LandingPage() {
  const reports = await backendRead<Reports>("/merchant/reports", {
    generated: false,
    attack_scorecard: null,
    dispute_defence: null,
  });

  const attack = reports.attack_scorecard;
  const defence = reports.dispute_defence;

  return (
    <div className="min-h-dvh">
      <SiteHeader />
      <main>
        <Hero />
        <Mechanism />
        <Proof attack={attack} defence={defence} generated={reports.generated} />
        <Doors />
        <Connects />
        <NotBuilt />
        <UnderTheHood />
      </main>
      <SiteFooter />
    </div>
  );
}

function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-surface/85 backdrop-blur">
      <div className="mx-auto flex h-[60px] w-full max-w-[1180px] items-center justify-between px-5 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5">
          <Image
            src="/icon-192x192.png"
            alt=""
            width={28}
            height={28}
            className="rounded-[7px]"
            priority
          />
          <span className="text-[15px] font-semibold tracking-tight text-ink">Dwarpal</span>
        </Link>
        <nav className="flex items-center gap-2 sm:gap-5">
          <a
            href="#how"
            className="hidden text-[13px] text-muted transition-colors hover:text-ink sm:inline"
          >
            How it decides
          </a>
          <a
            href="#proof"
            className="hidden text-[13px] text-muted transition-colors hover:text-ink sm:inline"
          >
            The numbers
          </a>
          <a
            href="#limits"
            className="hidden text-[13px] text-muted transition-colors hover:text-ink sm:inline"
          >
            What is not built
          </a>
          <Link
            href="/login"
            className="rounded-[8px] bg-brand px-3.5 py-2 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong"
          >
            Open the console
          </Link>
        </nav>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-line bg-surface">
      <div className="pointer-events-none absolute inset-0 grid-field" aria-hidden="true" />
      <div className="relative mx-auto w-full max-w-[1180px] px-5 pb-16 pt-14 sm:px-6 sm:pb-20 sm:pt-20">
        <div className="grid items-center gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
          <div>
            <span className="inline-flex items-center gap-2 rounded-full border border-line bg-sunken px-3 py-1 text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
              AP2 merchant endpoint for Razorpay
            </span>

            <h1 className="display mt-6 text-[clamp(2.35rem,6vw,4.15rem)]">
              An agent just tried to
              <br />
              spend your money.
              <br />
              <span className="text-brand">Prove it was allowed to.</span>
            </h1>

            <p className="mt-6 max-w-[52ch] text-[15px] leading-relaxed text-body sm:text-[16px]">
              When a bot buys and the human later disputes it, there is no 3-D Secure record, no
              signed receipt and no accepted evidence standard. The merchant carries that loss.
              Dwarpal is the gate that stops the unauthorised purchase, and the evidence that
              defends the one that gets disputed anyway.
            </p>

            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Link
                href="/login"
                className="rounded-[9px] bg-brand px-5 py-3 text-[14px] font-medium text-white shadow-e1 transition-colors hover:bg-brand-strong"
              >
                Open the console
              </Link>
              <a
                href="#how"
                className="rounded-[9px] border border-line-strong bg-surface px-5 py-3 text-[14px] font-medium text-ink transition-colors hover:bg-sunken"
              >
                How a purchase is decided
              </a>
            </div>

            <dl className="mt-10 grid gap-x-8 gap-y-4 border-t border-line pt-6 sm:grid-cols-3">
              <Credential term="Conformance" detail="Validated against the published AP2 schemas" />
              <Credential term="Payments" detail="Razorpay test mode, enforced at startup" />
              <Credential term="Evidence" detail="Verifiable with the application stopped" />
            </dl>
          </div>

          <Reveal delay={80}>
            <LandingVideo src="/landing_page_video.mp4" />
            <p className="mt-3 text-center text-[12px] text-faint">
              One agent, one cart, four credentials, one verdict, one evidence packet.
            </p>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

function Credential({ term, detail }: { term: string; detail: string }) {
  return (
    <div>
      <dt className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{term}</dt>
      <dd className="mt-1 text-[13px] leading-snug text-body">{detail}</dd>
    </div>
  );
}

const STAGES = [
  {
    step: "1",
    name: "Verification",
    tone: "text-brand",
    body: "Seven checks in a fixed order, refusing at the first failure: well formed and signed, the key it was issued to, a trusted issuer, inside its window, never seen before, bound to the Checkout the merchant signed, and inside the authority the human granted.",
    refuses: "CRED_SIGNATURE_INVALID, CRED_SUBJECT_MISMATCH, CRED_REPLAYED",
  },
  {
    step: "2",
    name: "Policy kernel",
    tone: "text-allow",
    body: "Deterministic and reason-coded. Kill switch, revocation, policy hash, item policy, tier ceiling, constraint satisfaction, agent limits, structuring, budget. It is the only stage that can approve, and no model is reachable from it.",
    refuses: "BUDGET_EXCEEDED, STRUCTURING_DETECTED, ITEM_AGE_RESTRICTED",
  },
  {
    step: "3",
    name: "Semantic check",
    tone: "text-escalate",
    body: "Only for a constraint arithmetic cannot settle. A price cap is subtraction; nothing perishable is not. The model has two outcomes and neither of them is approval, so a jailbroken model can only ever cost a human question.",
    refuses: "SEMANTIC_DENIED, or an escalation",
  },
  {
    step: "4",
    name: "The human",
    tone: "text-challenge",
    body: "Approve or deny over WhatsApp, against a deadline. Silence is a denial, an answer counts once, and an approval covers exactly the cart it was raised for. Change the cart and the approval is void.",
    refuses: "ESCALATION_TIMEOUT, ESCALATION_DENIED",
  },
];

function Mechanism() {
  return (
    <section id="how" className="border-b border-line bg-canvas py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="How it decides"
            title="Four stages. Every one of them can refuse."
            lede="Only the kernel can approve on its own. The other three exist to resolve what it could not, and each of them resolves towards refusing."
          />
        </Reveal>

        <div className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {STAGES.map((stage, index) => (
            <Reveal key={stage.name} delay={index * 70}>
              <div className="flex h-full flex-col rounded-[14px] border border-line bg-surface p-5 shadow-e1">
                <div className="flex items-baseline gap-2.5">
                  <span className={`font-mono text-[13px] font-semibold ${stage.tone}`}>
                    {stage.step}
                  </span>
                  <h3 className="text-[15px] font-semibold tracking-tight text-ink">
                    {stage.name}
                  </h3>
                </div>
                <p className="mt-3 flex-1 text-[13px] leading-relaxed text-body">{stage.body}</p>
                <div className="mt-4 border-t border-line pt-3">
                  <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-faint">
                    Refuses with
                  </div>
                  <div className="mt-1.5 font-mono text-[11px] leading-relaxed text-deny">
                    {stage.refuses}
                  </div>
                </div>
              </div>
            </Reveal>
          ))}
        </div>

        <Reveal delay={120}>
          <div className="mt-6 rounded-[14px] border border-line bg-surface px-5 py-5 shadow-e1 sm:px-6">
            <p className="text-[14px] leading-relaxed text-body">
              <span className="font-medium text-ink">Everything uncertain resolves downward.</span>{" "}
              An unparseable credential, an unknown authority, an expired mandate, a constraint
              arithmetic cannot settle, a model that is unsure, a human who does not answer, an
              unreachable gateway, an undeclared region for a region-locked item. None of them
              produces an approval. That is the whole design, stated once.
            </p>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Proof({
  attack,
  defence,
  generated,
}: {
  attack: Reports["attack_scorecard"];
  defence: Reports["dispute_defence"];
  generated: boolean;
}) {
  const adversarial = attack?.adversarial;
  const benign = attack?.benign;

  return (
    <section id="proof" className="border-b border-line bg-surface py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="The numbers"
            title="Both halves, always together."
            lede="A gate that refuses everything scores perfectly against attacks and is useless, so the block rate is never shown without the false-positive rate beside it. These are read from the running merchant, not written into this page."
          />
        </Reveal>

        {generated && adversarial && benign ? (
          <>
            <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Figure
                value={`${adversarial.blocked}/${adversarial.total}`}
                label="Attacks blocked"
                tone="text-allow"
              />
              <Figure
                value={String(adversarial.missed)}
                label="Attacks missed"
                tone={adversarial.missed > 0 ? "text-deny" : "text-ink"}
                hint="named in the report, never summarised away"
              />
              <Figure
                value={`${benign.false_positives}/${benign.total}`}
                label="Legitimate traffic wrongly refused"
                tone={benign.false_positives > 0 ? "text-deny" : "text-allow"}
              />
              <Figure
                value={
                  defence ? `+${Math.round((defence.improvement ?? 0) * 100)}%` : "-"
                }
                label="Disputes defensible, with evidence against without"
                tone="text-brand"
              />
            </div>
            <p className="mt-5 text-[12px] text-faint">
              Regenerated by <code className="font-mono">python -m app.cli reports</code>. The
              escalations in the benign corpus are counted separately from false positives: asking
              the principal about a constraint the kernel cannot decide is the designed behaviour,
              not an error.
            </p>
          </>
        ) : (
          <div className="mt-10 rounded-[14px] border border-line bg-canvas px-5 py-8 text-center sm:px-6">
            <p className="text-[14px] text-body">
              No scorecard has been generated on this machine yet.
            </p>
            <p className="mt-2 text-[13px] text-muted">
              Run <code className="font-mono text-ink">python -m app.cli reports</code> from the
              backend directory and this section fills itself in from the artifact it writes.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

function Figure({ value, label, tone, hint }: { value: string; label: string; tone: string; hint?: string }) {
  return (
    <div className="rounded-[14px] border border-line bg-canvas px-5 py-5">
      <div className={`text-[30px] font-semibold tabular-nums leading-none ${tone}`}>{value}</div>
      <div className="mt-2.5 text-[13px] leading-snug text-body">{label}</div>
      {hint && <div className="mt-1.5 text-[11px] leading-snug text-faint">{hint}</div>}
    </div>
  );
}

function Doors() {
  return (
    <section className="border-b border-line bg-canvas py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="Two consoles"
            title="Watch an agent spend, or watch the gate decide."
            lede="Both run against the same merchant. There is no sign-up and no password; the login page picks a view."
          />
        </Reveal>

        <div className="mt-10 grid gap-4 lg:grid-cols-2">
          <Reveal>
            <DoorCard
              eyebrow="Buyer"
              title="Send an agent shopping"
              body="Ask in plain language. The agent reads the catalog, the trusted surface signs the standing authority, the agent signs the claim about this purchase, and the merchant decides. Pay with the Razorpay test card; nothing else is accepted."
              points={[
                "A live log of every step the agent took",
                "The four AP2 credentials, with their digests",
                "The verdict, its reason code and its evidence packet",
              ]}
            />
          </Reveal>
          <Reveal delay={80}>
            <DoorCard
              eyebrow="Merchant"
              title="Run the gate"
              body="Every decision the kernel made, with the reason code and the evidence behind it. Refusals are shown as prominently as approvals, because the refusals are the record that defends a dispute."
              points={[
                "Live traffic, budgets and per-agent limits",
                "Mandates in force, with revocation",
                "Hash-chained evidence and the disputes it wins",
              ]}
              primary
            />
          </Reveal>
        </div>
      </div>
    </section>
  );
}

function DoorCard({
  eyebrow,
  title,
  body,
  points,
  primary = false,
}: {
  eyebrow: string;
  title: string;
  body: string;
  points: string[];
  primary?: boolean;
}) {
  return (
    <div className="flex h-full flex-col rounded-[16px] border border-line bg-surface p-6 shadow-e1 transition-shadow duration-300 hover:shadow-e2 sm:p-7">
      <div className="text-[11px] font-semibold uppercase tracking-[0.09em] text-faint">
        {eyebrow}
      </div>
      <h3 className="mt-2 text-[21px] font-semibold tracking-[-0.015em] text-ink">{title}</h3>
      <p className="mt-3 text-[13px] leading-relaxed text-body">{body}</p>
      <ul className="mt-5 flex-1 space-y-2.5">
        {points.map((point) => (
          <li key={point} className="flex gap-2.5 text-[13px] leading-relaxed text-body">
            <svg
              viewBox="0 0 16 16"
              className="mt-[3px] h-3.5 w-3.5 shrink-0 text-brand"
              aria-hidden="true"
            >
              <path
                d="M3 8.5l3.2 3.2L13 5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            {point}
          </li>
        ))}
      </ul>
      <Link
        href="/login"
        className={`mt-6 inline-flex justify-center rounded-[9px] px-4 py-2.5 text-[13px] font-medium transition-colors ${
          primary
            ? "bg-brand text-white hover:bg-brand-strong"
            : "border border-line-strong bg-surface text-ink hover:bg-sunken"
        }`}
      >
        Open this console
      </Link>
    </div>
  );
}

const LIMITS = [
  {
    title: "The Credential Provider is mocked",
    body: "AP2 puts credential issuance outside the merchant's role, so Dwarpal does not implement one. The trusted surface and the credential provider in every demo here are a test harness, and the conformance matrix in the README says so rather than implying otherwise.",
  },
  {
    title: "The human-present flow is not implemented",
    body: "That is the flow where a person is at the keyboard and the merchant's verification duty is largely discharged by the checkout page. The interesting risk is all in human-not-present, so that is what is built.",
  },
  {
    title: "No UAP compliance is claimed",
    body: "Dwarpal is designed for NPCI's Unified Agent Protocol, which is in development and unpublished. Designed for UAP, compliant with AP2, and never a word more than that.",
  },
  {
    title: "The approved WhatsApp template does not exist",
    body: "Escalations and receipts deliver, and a tapped Approve reaches the webhook and settles the escalation. What is missing is the approved Utility template the escalation prefers: it is configured by name but was never created, so every send falls back to a free-form message. That works only inside the 24 hour window after the person last messaged the business number. Outside it, an escalation would not be delivered at all, and would become a denial at its deadline.",
  },
];

function Connects() {
  return (
    <section id="connect" className="border-b border-line bg-surface py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="Bring your own agent"
            title="It connects to what you already use."
            lede="An assistant reads the catalog over MCP. The human hears about the purchase on WhatsApp. Neither of them can widen what the kernel allowed."
          />
        </Reveal>

        <div className="mt-10 grid gap-4 lg:grid-cols-2">
          <Reveal>
            <IntegrationCard
              mark={<ClaudeMark colored className="h-7 w-7" title="Claude" />}
              tint={CLAUDE_ORANGE}
              eyebrow="Model Context Protocol"
              title="Point Claude at the merchant"
              body="Claude Desktop and Claude Code connect over stdio or streamable HTTP and get seven tools: browse, search, fetch an item, list categories, read the signed policy terms, describe the merchant, and take a quote."
              points={[
                "Read-and-quote only; settling still runs the full verification pipeline",
                "quote_cart returns the merchant-signed Checkout, digests and all",
                "Every item carries machine-readable purchase constraints",
              ]}
            />
          </Reveal>
          <Reveal delay={80}>
            <IntegrationCard
              mark={<WhatsAppMark colored className="h-7 w-7" title="WhatsApp" />}
              tint={WHATSAPP_GREEN}
              eyebrow="WhatsApp Cloud API"
              title="Tell the human what happened"
              body="A receipt when an agent buys, the reason code when it is refused, and a refund notice when a capture had to be reversed. When the kernel cannot decide alone, the escalation arrives with Approve and Deny buttons."
              points={[
                "Only ever to a number registered on a connection",
                "An escalation nobody answers is a denial, never a pause",
                "Every send is logged with its route and status, failures included",
              ]}
            />
          </Reveal>
        </div>

        <Reveal delay={140}>
          <p className="mt-8 text-[13px] leading-relaxed text-muted">
            The exact configuration, both transports and the settings each one needs are on{" "}
            <Link href="/buyer/setup" className="text-brand hover:underline">
              the agent setup page
            </Link>
            .
          </p>
        </Reveal>
      </div>
    </section>
  );
}

function IntegrationCard({
  mark,
  tint,
  eyebrow,
  title,
  body,
  points,
}: {
  mark: React.ReactNode;
  tint: string;
  eyebrow: string;
  title: string;
  body: string;
  points: string[];
}) {
  return (
    <div className="flex h-full flex-col rounded-[16px] border border-line bg-canvas p-6 shadow-e1 sm:p-7">
      <BrandHeading mark={mark} tint={tint} eyebrow={eyebrow} title={title} />
      <p className="mt-4 text-[13.5px] leading-relaxed text-body">{body}</p>
      <ul className="mt-5 space-y-2.5 border-t border-line pt-5">
        {points.map((point) => (
          <li key={point} className="flex gap-2.5 text-[12.5px] leading-relaxed text-muted">
            <span
              aria-hidden="true"
              className="mt-[7px] h-[5px] w-[5px] shrink-0 rounded-full"
              style={{ backgroundColor: tint }}
            />
            <span>{point}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function NotBuilt() {
  return (
    <section id="limits" className="border-b border-line bg-surface py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="What is not built"
            title="The parts that would be easiest to imply."
            lede="A system that gates money is only worth as much as its most honest claim, so the gaps are on the front page rather than in a footnote."
          />
        </Reveal>

        <div className="mt-10 grid gap-4 md:grid-cols-2">
          {LIMITS.map((limit, index) => (
            <Reveal key={limit.title} delay={index * 60}>
              <div className="h-full rounded-[14px] border border-line bg-canvas p-5">
                <h3 className="text-[14px] font-semibold tracking-tight text-ink">{limit.title}</h3>
                <p className="mt-2 text-[13px] leading-relaxed text-muted">{limit.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

const COMMANDS: [string, string][] = [
  ["Start everything", "docker compose up -d && cd backend && uvicorn main:app --reload"],
  ["Drive one purchase", "python interop/run_interop.py"],
  ["Fill the dashboard", "python scenarios/run_suite.py --profile demo"],
  ["Regenerate the numbers", "python -m app.cli reports"],
  [
    "Verify the evidence offline",
    "python tools/verify_evidence.py --jsonl reports/evidence.jsonl --jwks reports/merchant_jwks.json",
  ],
];

function UnderTheHood() {
  return (
    <section className="bg-canvas py-16 sm:py-20">
      <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-6">
        <Reveal>
          <SectionHeading
            eyebrow="Under the hood"
            title="Five commands from a clone to a full dashboard."
          />
        </Reveal>

        <Reveal delay={60}>
          <div className="mt-8 overflow-hidden rounded-[14px] border border-line bg-surface shadow-e1">
            {COMMANDS.map(([label, command], index) => (
              <div
                key={label}
                className={`flex flex-col gap-1.5 px-5 py-4 sm:flex-row sm:items-center sm:gap-6 ${
                  index > 0 ? "border-t border-line" : ""
                }`}
              >
                <div className="w-[190px] shrink-0 text-[13px] font-medium text-ink">{label}</div>
                <code className="scroll-x block whitespace-pre font-mono text-[12px] text-body">
                  {command}
                </code>
              </div>
            ))}
          </div>
        </Reveal>

        <Reveal delay={100}>
          <div className="mt-6 grid gap-3 text-[13px] text-muted sm:grid-cols-3">
            <TechNote label="Backend" value="FastAPI, Python 3.12, PostgreSQL" />
            <TechNote label="Frontend" value="Next.js App Router, TypeScript, Tailwind" />
            <TechNote label="Model" value="Gemini, on one path, able only to deny or escalate" />
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function TechNote({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[10px] border border-line bg-surface px-4 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-faint">
        {label}
      </div>
      <div className="mt-1 text-[13px] leading-snug text-body">{value}</div>
    </div>
  );
}

function SectionHeading({
  eyebrow,
  title,
  lede,
}: {
  eyebrow: string;
  title: string;
  lede?: string;
}) {
  return (
    <div className="max-w-[64ch]">
      <div className="text-[11px] font-semibold uppercase tracking-[0.09em] text-brand">
        {eyebrow}
      </div>
      <h2 className="mt-3 text-[clamp(1.6rem,3.4vw,2.35rem)] font-semibold tracking-[-0.02em] leading-[1.12] text-ink">
        {title}
      </h2>
      {lede && <p className="mt-3 text-[14px] leading-relaxed text-muted">{lede}</p>}
    </div>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-line bg-surface">
      <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-4 px-5 py-8 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div className="flex items-center gap-2.5">
          <Image src="/icon-192x192.png" alt="" width={24} height={24} className="rounded-[6px]" />
          <span className="text-[13px] text-muted">
            Dwarpal, the AP2 merchant endpoint for Razorpay
          </span>
        </div>
        <div className="flex items-center gap-5 text-[12px] text-faint">
          <span>Designed for UAP, compliant with AP2</span>
          <Link href="/login" className="text-muted transition-colors hover:text-ink">
            Open the console
          </Link>
        </div>
      </div>
    </footer>
  );
}
