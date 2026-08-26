import type { Metadata } from "next";
import Link from "next/link";

import { startRun } from "@/app/buyer-actions";
import { CopyField } from "@/components/copy";
import { BackendDown, Card, Code, Note, PageHeader, Pill } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative } from "@/lib/format";
import type { BuyerDefaults, BuyerRunSummary, GatewayMode } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Send an agent" };

export default async function BuyerPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const [defaults, gateway, runs] = await Promise.all([
    backendRead<BuyerDefaults>("/buyer/defaults", {
      budget_cap_minor: 2_000_000,
      suggested_prompts: [],
      constraints: [],
    }),
    backendRead<GatewayMode | null>("/buyer/gateway", null),
    backendRead<{ runs: BuyerRunSummary[] }>("/buyer/runs?limit=5", { runs: [] }),
  ]);

  return (
    <>
      <PageHeader
        title="Send an agent shopping"
        description="Ask in plain language. The agent reads the merchant's catalog, a trusted surface signs the standing authority on your behalf, the agent signs the claim about this specific purchase, and the merchant decides."
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <Card title="What should it buy?">
          <form action={startRun} className="space-y-5 px-4 py-5 sm:px-5">
            <div>
              <label
                htmlFor="prompt"
                className="mb-1.5 block text-[12px] font-medium text-ink"
              >
                Your instruction
              </label>
              <textarea
                id="prompt"
                name="prompt"
                required
                rows={3}
                defaultValue={defaults.suggested_prompts[0] ?? ""}
                placeholder="Buy me two packets of tea and a notebook, under 2000 rupees"
                className="w-full resize-y rounded-[9px] border border-line bg-surface px-3 py-2.5 text-[13px] leading-relaxed text-ink placeholder:text-faint focus:border-brand"
              />
              {defaults.suggested_prompts.length > 1 && (
                <p className="mt-2 text-[11.5px] leading-relaxed text-faint">
                  Also try: {defaults.suggested_prompts.slice(1).join(" / ")}
                </p>
              )}
            </div>

            <div>
              <label htmlFor="budget" className="mb-1.5 block text-[12px] font-medium text-ink">
                Budget cap, in rupees
              </label>
              <input
                id="budget"
                name="budget"
                type="number"
                min={1}
                step={1}
                defaultValue={Math.round(defaults.budget_cap_minor / 100)}
                className="w-full rounded-[9px] border border-line bg-surface px-3 py-2.5 text-[13px] tabular-nums text-ink focus:border-brand sm:max-w-[220px]"
              />
              <p className="mt-1.5 text-[11.5px] leading-relaxed text-faint">
                This becomes the cap inside the open Payment Mandate. The kernel enforces it
                arithmetically, and the agent cannot widen it.
              </p>
            </div>

            <fieldset>
              <legend className="mb-2 text-[12px] font-medium text-ink">
                Standing instructions the kernel cannot settle on its own
              </legend>
              <div className="flex flex-wrap gap-2">
                {defaults.constraints.map((constraint) => (
                  <label
                    key={constraint}
                    className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-line bg-surface px-3 py-1.5 text-[12px] text-body transition-colors hover:bg-sunken has-checked:border-brand has-checked:bg-brand-tint has-checked:text-brand-strong"
                  >
                    <input
                      type="checkbox"
                      name="constraint"
                      value={constraint}
                      className="h-3.5 w-3.5 accent-[color:var(--brand)]"
                    />
                    {constraint}
                  </label>
                ))}
              </div>
              <p className="mt-2 text-[11.5px] leading-relaxed text-faint">
                These are the ones that go to the model and then to you. Tick one and the purchase
                will not complete on its own, which is the behaviour, not a bug.
              </p>
            </fieldset>

            <fieldset>
              <legend className="mb-2 text-[12px] font-medium text-ink">Which AP2 flow</legend>
              <label className="inline-flex cursor-pointer items-start gap-2.5 rounded-[9px] border border-line bg-surface px-3 py-2.5 text-[12px] text-body transition-colors hover:bg-sunken has-checked:border-brand has-checked:bg-brand-tint">
                <input
                  type="checkbox"
                  name="human_present"
                  className="mt-0.5 h-3.5 w-3.5 accent-[color:var(--brand)]"
                />
                <span>
                  <span className="font-medium text-ink">I am at the keyboard for this one</span>
                  <span className="mt-1 block text-[11.5px] leading-relaxed text-faint">
                    Switches to AP2&apos;s human-present flow. The trusted surface signs an
                    attestation bound to this exact cart, and the merchant verifies it like any
                    other credential: right signer, right cart, recent, and usable once. It changes
                    nothing about what you are allowed to buy. Leave it unticked and the run is
                    human-not-present, which is the flow everything else here is about.
                  </span>
                </span>
              </label>
            </fieldset>

            <button
              type="submit"
              className="w-full rounded-[9px] bg-brand px-4 py-3 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong sm:w-auto sm:px-6"
            >
              Send the agent
            </button>
          </form>
        </Card>

        <div className="space-y-6">
          <Card
            title="The card to pay with"
            description="Razorpay test mode. These details are published by Razorpay and are only accepted against a test key."
          >
            <div className="space-y-3 px-4 py-5 sm:px-5">
              {gateway ? (
                <>
                  <CopyField label="Card number" value={gateway.test_card.number} />
                  <div className="grid grid-cols-2 gap-3">
                    <CopyField label="Expiry" value={gateway.test_card.expiry} />
                    <CopyField label="CVV" value={gateway.test_card.cvv} />
                  </div>
                  <Note tone={gateway.mode === "razorpay" ? "brand" : "escalate"}>
                    <span className="font-medium">
                      {gateway.mode === "razorpay"
                        ? "Razorpay test-mode Checkout is live here."
                        : "This merchant is running the stub gateway."}
                    </span>{" "}
                    {gateway.explanation}
                  </Note>
                </>
              ) : (
                <p className="text-[13px] text-muted">
                  The merchant did not report its gateway configuration.
                </p>
              )}
              <p className="text-[11.5px] leading-relaxed text-faint">
                Dwarpal refuses to start against a live Razorpay key. A defect on the checkout path
                could otherwise move real money, so the check is made at startup rather than left
                to whoever is deploying it.
              </p>
            </div>
          </Card>

          <Card title="Recent runs">
            {runs.runs.length === 0 ? (
              <div className="px-5 py-8 text-center text-[13px] text-muted">
                No agent has been sent yet.
              </div>
            ) : (
              <ul className="divide-y divide-[color:var(--line)]">
                {runs.runs.map((run) => (
                  <li key={run.id}>
                    <Link
                      href={`/buyer/runs/${run.id}`}
                      className="flex items-start justify-between gap-3 px-4 py-3 transition-colors hover:bg-sunken sm:px-5"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-[13px] text-ink">{run.prompt}</span>
                        <span className="mt-1 block text-[11.5px] text-faint">
                          {relative(run.created_at)} - {run.amount.display}
                        </span>
                      </span>
                      <RunStatus status={run.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <Card title="What actually happens when you press send">
        <ol className="space-y-3 px-4 py-5 text-[13px] leading-relaxed text-body sm:px-5">
          <Step n={1}>
            An agent identity is minted and its issuing authority publishes the matching public key
            where the merchant will look for it.
          </Step>
          <Step n={2}>
            A planner turns your sentence into a cart. Whatever it proposes is re-read from the
            catalog before anything is quoted, so an invented SKU or price never reaches the wire.
          </Step>
          <Step n={3}>
            The merchant freezes the prices, holds the stock and signs a Checkout it commits to
            fulfil. That signature is what every later step is compared against.
          </Step>
          <Step n={4}>
            A trusted surface signs the two open mandates: what may be bought, and how it may be
            paid. The agent signs the two closed ones and proves it holds the key they were issued
            to.
          </Step>
          <Step n={5}>
            All four go to <Code>POST /checkout/complete</Code>. Verification, then the kernel, then
            the model if a prose constraint is unresolved, then you.
          </Step>
          <Step n={6}>
            Whatever the outcome, an evidence packet is written and the chain is extended. Refusals
            are filed too; they are the more valuable record.
          </Step>
        </ol>
      </Card>
    </>
  );
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-brand-tint font-mono text-[11px] font-semibold text-brand-strong">
        {n}
      </span>
      <span>{children}</span>
    </li>
  );
}

const STATUS_TONE: Record<string, "allow" | "deny" | "escalate" | "neutral" | "brand"> = {
  completed: "allow",
  awaiting_payment: "brand",
  escalated: "escalate",
  compensated: "escalate",
  refused: "deny",
  error: "deny",
};

export function RunStatus({ status }: { status: string }) {
  return <Pill tone={STATUS_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Pill>;
}
