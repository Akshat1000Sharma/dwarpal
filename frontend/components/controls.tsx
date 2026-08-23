"use client";

import { useState, useTransition } from "react";

import type { ActionResult } from "@/app/actions";

/** The merchant's write controls. The dashboard is otherwise read-only. */

function Feedback({ result }: { result: ActionResult | null }) {
  if (!result) return null;
  return (
    <p className={`mt-2 text-xs ${result.ok ? "text-allow" : "text-deny"}`}>{result.message}</p>
  );
}

export function RevokeMandate({
  mandateId,
  revoked,
  action,
}: {
  mandateId: string;
  revoked: boolean;
  action: (mandateId: string, reason: string) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);

  if (revoked) {
    return <span className="text-xs text-muted">already revoked</span>;
  }

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="rounded border border-deny px-2 py-1 text-xs text-deny hover:bg-deny-bg"
      >
        Revoke
      </button>
    );
  }

  return (
    <div className="min-w-[220px]">
      <input
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Reason recorded in the evidence"
        className="w-full rounded border border-line bg-surface px-2 py-1 text-xs"
      />
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={() =>
            start(async () => {
              setResult(await action(mandateId, reason));
              setConfirming(false);
            })
          }
          className="rounded bg-deny px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
        >
          {pending ? "Revoking..." : "Confirm revoke"}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          className="rounded border border-line px-2 py-1 text-xs text-muted"
        >
          Cancel
        </button>
      </div>
      <Feedback result={result} />
    </div>
  );
}

export function KillSwitch({
  agentId,
  enabled,
  action,
}: {
  agentId: string;
  enabled: boolean;
  action: (agentId: string, enabled: boolean) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div>
      <button
        type="button"
        disabled={pending}
        onClick={() =>
          start(async () => {
            setResult(await action(agentId, !enabled));
          })
        }
        className={`rounded px-2 py-1 text-xs font-medium disabled:opacity-50 ${
          enabled
            ? "bg-deny text-white"
            : "border border-line text-muted hover:text-foreground"
        }`}
      >
        {pending ? "Saving..." : enabled ? "Stopped, click to allow" : "Stop this agent"}
      </button>
      <Feedback result={result} />
    </div>
  );
}

export function ResolveException({
  exceptionId,
  action,
}: {
  exceptionId: string;
  action: (exceptionId: string) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div>
      <button
        type="button"
        disabled={pending}
        onClick={() =>
          start(async () => {
            setResult(await action(exceptionId));
          })
        }
        className="rounded border border-line px-2 py-1 text-xs text-muted hover:text-foreground disabled:opacity-50"
      >
        {pending ? "Saving..." : "Mark reconciled"}
      </button>
      <Feedback result={result} />
    </div>
  );
}

export function AgentLimits({
  agentId,
  spendMinor,
  transactions,
  action,
}: {
  agentId: string;
  spendMinor: number;
  transactions: number;
  action: (
    agentId: string,
    limits: { max_spend_per_window_minor?: number; max_transactions_per_window?: number },
  ) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [spend, setSpend] = useState(String(spendMinor / 100));
  const [count, setCount] = useState(String(transactions));

  return (
    <div className="min-w-[240px]">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted">
          Spend INR
          <input
            value={spend}
            onChange={(event) => setSpend(event.target.value)}
            inputMode="decimal"
            className="ml-1 w-24 rounded border border-line bg-surface px-2 py-1 text-xs text-foreground"
          />
        </label>
        <label className="text-xs text-muted">
          Count
          <input
            value={count}
            onChange={(event) => setCount(event.target.value)}
            inputMode="numeric"
            className="ml-1 w-16 rounded border border-line bg-surface px-2 py-1 text-xs text-foreground"
          />
        </label>
        <button
          type="button"
          disabled={pending}
          onClick={() =>
            start(async () => {
              const major = Number.parseFloat(spend);
              const times = Number.parseInt(count, 10);
              if (Number.isNaN(major) || Number.isNaN(times)) {
                setResult({ ok: false, message: "Both limits must be numbers." });
                return;
              }
              setResult(
                await action(agentId, {
                  max_spend_per_window_minor: Math.round(major * 100),
                  max_transactions_per_window: times,
                }),
              );
            })
          }
          className="rounded border border-line px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
        >
          {pending ? "Saving..." : "Save"}
        </button>
      </div>
      <Feedback result={result} />
    </div>
  );
}

export function CategoryGate({
  agentId,
  categories,
  blocked,
  action,
}: {
  agentId: string;
  categories: string[];
  blocked: string[];
  action: (agentId: string, limits: { blocked_categories?: string[] }) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [selected, setSelected] = useState<string[]>(blocked);

  const toggle = (category: string) => {
    const next = selected.includes(category)
      ? selected.filter((item) => item !== category)
      : [...selected, category];
    setSelected(next);
    start(async () => {
      setResult(await action(agentId, { blocked_categories: next }));
    });
  };

  return (
    <div className="min-w-[220px]">
      <div className="flex flex-wrap gap-1">
        {categories.map((category) => {
          const on = selected.includes(category);
          return (
            <button
              key={category}
              type="button"
              disabled={pending}
              onClick={() => toggle(category)}
              className={`rounded px-2 py-0.5 text-xs disabled:opacity-50 ${
                on ? "bg-deny-bg text-deny" : "bg-surface-muted text-muted hover:text-foreground"
              }`}
            >
              {on ? "blocked: " : ""}
              {category}
            </button>
          );
        })}
      </div>
      <Feedback result={result} />
    </div>
  );
}

export function OpenDispute({
  correlationId,
  action,
}: {
  correlationId: string;
  action: (correlationId: string, claim: string) => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [claim, setClaim] = useState("");

  return (
    <div className="min-w-[260px]">
      <input
        value={claim}
        onChange={(event) => setClaim(event.target.value)}
        placeholder="What the cardholder claims"
        className="w-full rounded border border-line bg-surface px-2 py-1 text-xs"
      />
      <button
        type="button"
        disabled={pending}
        onClick={() =>
          start(async () => {
            setResult(await action(correlationId, claim));
          })
        }
        className="mt-2 rounded border border-line px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
      >
        {pending ? "Assembling..." : "Assemble representment"}
      </button>
      <Feedback result={result} />
    </div>
  );
}

export function DisputeDecision({
  disputeId,
  action,
}: {
  disputeId: string;
  action: (disputeId: string, outcome: "contested" | "refunded") => Promise<ActionResult>;
}) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  return (
    <div>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={() => start(async () => setResult(await action(disputeId, "contested")))}
          className="rounded border border-line px-3 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
        >
          Record as contested
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => start(async () => setResult(await action(disputeId, "refunded")))}
          className="rounded border border-line px-3 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
        >
          Record as refunded
        </button>
      </div>
      <Feedback result={result} />
    </div>
  );
}
