import type { ReactNode } from "react";

/**
 * Shared presentation pieces.
 *
 * Two rules run through all of it. Refusals are rendered as prominently as approvals, because a
 * merchant's refusals are the more valuable record. And every table collapses into labelled cards
 * below the medium breakpoint rather than forcing a sideways scroll, because a control plane you
 * cannot read on a phone is a control plane you will not check.
 */

export type Tone = "neutral" | "allow" | "deny" | "escalate" | "challenge" | "brand";

const TEXT_TONE: Record<Tone, string> = {
  neutral: "text-ink",
  allow: "text-allow",
  deny: "text-deny",
  escalate: "text-escalate",
  challenge: "text-challenge",
  brand: "text-brand",
};

const FILL_TONE: Record<Tone, string> = {
  neutral: "bg-sunken text-muted",
  allow: "bg-allow-bg text-allow",
  deny: "bg-deny-bg text-deny",
  escalate: "bg-escalate-bg text-escalate",
  challenge: "bg-challenge-bg text-challenge",
  brand: "bg-brand-tint text-brand-strong",
};

export function Card({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`overflow-hidden rounded-[14px] border border-line bg-surface shadow-e1 ${className}`}
    >
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-4 sm:px-5">
          <div className="min-w-0">
            {title && (
              <h2 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h2>
            )}
            {description && (
              <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-muted">{description}</p>
            )}
          </div>
          {actions && <div className="shrink-0">{actions}</div>}
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
  tone?: Tone;
}) {
  return (
    <div className="rounded-[14px] border border-line bg-surface px-4 py-4 shadow-e1">
      <div className="text-[12px] font-medium uppercase tracking-[0.06em] text-faint">{label}</div>
      <div className={`mt-2 text-[26px] font-semibold tabular-nums leading-none ${TEXT_TONE[tone]}`}>
        {value}
      </div>
      {hint && <div className="mt-2 text-[12px] leading-snug text-muted">{hint}</div>}
    </div>
  );
}

const DECISION_TONE: Record<string, Tone> = {
  allow: "allow",
  deny: "deny",
  escalate: "escalate",
  challenge: "challenge",
};

export function DecisionBadge({ decision }: { decision: string }) {
  const tone = DECISION_TONE[decision] ?? "neutral";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.04em] ${FILL_TONE[tone]}`}
    >
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
      className={`inline-block rounded-[6px] px-2 py-0.5 font-mono text-[11px] ${
        approved ? "bg-allow-bg text-allow" : "bg-deny-bg text-deny"
      }`}
    >
      {code}
    </span>
  );
}

export function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-medium ${FILL_TONE[tone]}`}
    >
      {children}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="px-5 py-12 text-center text-[13px] leading-relaxed text-muted">{children}</div>
  );
}

/**
 * Tables carry their column labels into the stacked layout via a data attribute, so the same
 * markup serves both. Below `md` each row becomes a card and each cell shows its own label.
 */
export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="scroll-x">
      <table className="w-full text-left text-[13px] md:min-w-[720px]">
        <thead className="hidden md:table-header-group">
          <tr className="border-b border-line text-[11px] uppercase tracking-[0.06em] text-faint">
            {head.map((column) => (
              <th key={column} scope="col" className="px-4 py-3 font-medium sm:px-5">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="block md:table-row-group">{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return (
    <tr className="block border-b border-line px-4 py-3 last:border-0 align-top md:table-row md:px-0 md:py-0">
      {children}
    </tr>
  );
}

export function Cell({
  children,
  mono = false,
  label,
}: {
  children: ReactNode;
  mono?: boolean;
  label?: string;
}) {
  return (
    <td
      className={`flex items-baseline justify-between gap-4 py-1.5 md:table-cell md:px-4 md:py-3.5 md:before:content-none sm:md:px-5 ${
        mono ? "font-mono text-[12px]" : ""
      }`}
    >
      {label && (
        <span className="shrink-0 font-sans text-[11px] uppercase tracking-[0.06em] text-faint md:hidden">
          {label}
        </span>
      )}
      <span className="min-w-0 text-right md:text-left">{children}</span>
    </td>
  );
}

export function Meter({ used, total, label }: { used: number; total: number; label?: string }) {
  const ratio = total > 0 ? Math.min(1, used / total) : 0;
  const tone = ratio > 0.9 ? "bg-deny" : ratio > 0.7 ? "bg-escalate" : "bg-allow";
  return (
    <div className="min-w-[140px]">
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-sunken"
        role="progressbar"
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label ?? "budget used"}
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      {label && <div className="mt-1.5 text-[11px] text-muted">{label}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="scroll-x rounded-[10px] border border-line bg-sunken p-3 text-[12px] leading-relaxed text-body">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function BackendDown({ detail }: { detail?: string }) {
  return (
    <Card title="The backend is not reachable">
      <div className="space-y-4 px-5 py-6 text-[13px]">
        <p className="text-muted">
          The dashboard proxies to the origin named by BACKEND_ORIGIN and got no usable answer.
        </p>
        <ol className="list-decimal space-y-2 pl-5 text-muted">
          <li>
            Start PostgreSQL from the repository root: <Code>docker compose up -d</Code>
          </li>
          <li>
            Start the backend: <Code>cd backend &amp;&amp; uvicorn main:app --reload</Code>
          </li>
          <li>
            Check BACKEND_ORIGIN in <Code>frontend/.env.local</Code>
          </li>
        </ol>
        {detail && <p className="font-mono text-[12px] text-deny">{detail}</p>}
      </div>
    </Card>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded-[5px] border border-line bg-sunken px-1.5 py-0.5 font-mono text-[12px] text-ink">
      {children}
    </code>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-[-0.015em] text-ink sm:text-[26px]">
          {title}
        </h1>
        {description && (
          <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-muted">{description}</p>
        )}
      </div>
      {actions && <div className="shrink-0">{actions}</div>}
    </div>
  );
}

export function Note({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  const border: Record<Tone, string> = {
    neutral: "border-line",
    allow: "border-allow/25",
    deny: "border-deny/25",
    escalate: "border-escalate/25",
    challenge: "border-challenge/25",
    brand: "border-brand/25",
  };
  return (
    <div
      className={`rounded-[10px] border px-4 py-3 text-[13px] leading-relaxed ${border[tone]} ${FILL_TONE[tone]}`}
    >
      {children}
    </div>
  );
}
