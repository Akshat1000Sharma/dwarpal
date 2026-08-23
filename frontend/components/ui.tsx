import type { ReactNode } from "react";

import { humanise } from "@/lib/format";

/** Shared presentation pieces. Refusals are rendered as prominently as approvals throughout. */

export function Card({
  title,
  description,
  actions,
  children,
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-line bg-surface">
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-tight">{title}</h2>}
            {description && <p className="mt-1 max-w-2xl text-xs text-muted">{description}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "neutral" | "allow" | "deny" | "escalate";
}) {
  const tones = {
    neutral: "text-foreground",
    allow: "text-allow",
    deny: "text-deny",
    escalate: "text-escalate",
  } as const;
  return (
    <div className="rounded-lg border border-line bg-surface px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${tones[tone]}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

const DECISION_STYLES: Record<string, string> = {
  allow: "bg-allow-bg text-allow",
  deny: "bg-deny-bg text-deny",
  escalate: "bg-escalate-bg text-escalate",
  challenge: "bg-challenge-bg text-challenge",
};

export function DecisionBadge({ decision }: { decision: string }) {
  const style = DECISION_STYLES[decision] ?? "bg-surface-muted text-muted";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-semibold uppercase ${style}`}>
      {decision}
    </span>
  );
}

const APPROVAL_CODES = new Set([
  "APPROVED",
  "APPROVED_WITHIN_UNVERIFIED_CEILING",
  "APPROVED_AFTER_HUMAN_APPROVAL",
]);

export function ReasonCode({ code }: { code: string }) {
  const approved = APPROVAL_CODES.has(code);
  return (
    <span
      title={code}
      className={`inline-block rounded px-2 py-0.5 font-mono text-xs ${
        approved ? "bg-allow-bg text-allow" : "bg-deny-bg text-deny"
      }`}
    >
      {code}
    </span>
  );
}

export function Pill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "allow" | "deny" | "escalate" | "challenge";
}) {
  const tones = {
    neutral: "bg-surface-muted text-muted",
    allow: "bg-allow-bg text-allow",
    deny: "bg-deny-bg text-deny",
    escalate: "bg-escalate-bg text-escalate",
    challenge: "bg-challenge-bg text-challenge",
  } as const;
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-5 py-10 text-center text-sm text-muted">{children}</div>;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="scroll-x">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead>
          <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
            {head.map((column) => (
              <th key={column} className="px-5 py-3 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return <tr className="border-b border-line last:border-0 align-top">{children}</tr>;
}

export function Cell({ children, mono = false }: { children: ReactNode; mono?: boolean }) {
  return <td className={`px-5 py-3 ${mono ? "font-mono text-xs" : ""}`}>{children}</td>;
}

export function Meter({ used, total, label }: { used: number; total: number; label?: string }) {
  const ratio = total > 0 ? Math.min(1, used / total) : 0;
  const tone = ratio > 0.9 ? "bg-deny" : ratio > 0.7 ? "bg-escalate" : "bg-allow";
  return (
    <div className="min-w-[140px]">
      <div className="h-1.5 w-full overflow-hidden rounded bg-surface-muted">
        <div className={`h-full ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      {label && <div className="mt-1 text-xs text-muted">{label}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="scroll-x rounded border border-line bg-surface-muted p-3 text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function BackendDown({ detail }: { detail?: string }) {
  return (
    <Card title="The backend is not reachable">
      <div className="space-y-3 px-5 py-6 text-sm">
        <p className="text-muted">
          The dashboard proxies to the origin named by BACKEND_ORIGIN and got no usable answer.
        </p>
        <ol className="list-decimal space-y-1 pl-5 text-muted">
          <li>
            Start PostgreSQL from the repository root: <code>docker compose up -d</code>
          </li>
          <li>
            Start the backend: <code>cd backend &amp;&amp; uvicorn main:app --reload</code>
          </li>
          <li>
            Check BACKEND_ORIGIN in <code>frontend/.env.local</code>
          </li>
        </ol>
        {detail && <p className="font-mono text-xs text-deny">{detail}</p>}
      </div>
    </Card>
  );
}

export function ReasonLabel({ code }: { code: string }) {
  return (
    <span className="text-xs text-muted" title={code}>
      {humanise(code)}
    </span>
  );
}
