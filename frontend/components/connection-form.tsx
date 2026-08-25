"use client";

import { useState, useTransition } from "react";

import { createConnection, revokeConnection } from "@/app/connection-actions";
import { CopyBlock, CopyField } from "@/components/copy";
import type { Connection } from "@/lib/types";

/**
 * Creating a connection, and the one moment its token exists.
 *
 * The token is shown once, here, and never again: only its digest is stored, so there is nothing
 * to show a second time. The form says that before you press the button rather than after.
 */
export function ConnectionForm({ header }: { header: string }) {
  const [pending, start] = useTransition();
  const [minted, setMinted] = useState<Connection | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (minted?.token) {
    return <Minted connection={minted} header={header} onDone={() => setMinted(null)} />;
  }

  return (
    <form
      action={(formData) =>
        start(async () => {
          setError(null);
          const result = await createConnection(formData);
          if (result.ok) setMinted(result.connection);
          else setError(result.message);
        })
      }
      className="space-y-4 px-4 py-5 sm:px-5"
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="label" className="mb-1.5 block text-[12px] font-medium text-ink">
            Label
          </label>
          <input
            id="label"
            name="label"
            required
            maxLength={120}
            placeholder="My Claude"
            className="w-full rounded-[9px] border border-line bg-surface px-3 py-2.5 text-[13px] text-ink placeholder:text-faint focus:border-brand"
          />
          <p className="mt-1.5 text-[11.5px] text-faint">So you can tell yours apart later.</p>
        </div>

        <div>
          <label htmlFor="whatsapp" className="mb-1.5 block text-[12px] font-medium text-ink">
            WhatsApp number
          </label>
          <input
            id="whatsapp"
            name="whatsapp"
            inputMode="tel"
            placeholder="+919876543210"
            className="w-full rounded-[9px] border border-line bg-surface px-3 py-2.5 font-mono text-[13px] text-ink placeholder:text-faint focus:border-brand"
          />
          <p className="mt-1.5 text-[11.5px] text-faint">
            E.164 form. Where the purchase receipts go.
          </p>
        </div>
      </div>

      <fieldset>
        <legend className="mb-2 text-[12px] font-medium text-ink">What is this agent for?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <ScopeOption
            value="buyer"
            title="Buying"
            body="Browse, quote and check out against the agent surface. Receipts for what it buys go to your number."
            defaultChecked
          />
          <ScopeOption
            value="merchant"
            title="Selling"
            body="Read verdicts and evidence, revoke a mandate, set an agent's limits, throw the kill switch."
          />
        </div>
      </fieldset>

      {error && (
        <p className="rounded-[9px] border border-deny/25 bg-deny-bg px-3 py-2 text-[12.5px] text-deny">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="rounded-[9px] bg-brand px-5 py-2.5 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong disabled:opacity-60"
      >
        {pending ? "Creating..." : "Create the connection"}
      </button>
      <p className="text-[11.5px] leading-relaxed text-faint">
        The token appears once, on the next screen. Only its digest is stored, so a lost token is
        replaced rather than recovered.
      </p>
    </form>
  );
}

function ScopeOption({
  value,
  title,
  body,
  defaultChecked = false,
}: {
  value: string;
  title: string;
  body: string;
  defaultChecked?: boolean;
}) {
  return (
    <label className="flex cursor-pointer gap-3 rounded-[10px] border border-line bg-surface p-3.5 transition-colors hover:bg-sunken has-checked:border-brand has-checked:bg-brand-tint">
      <input
        type="radio"
        name="scope"
        value={value}
        defaultChecked={defaultChecked}
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[color:var(--brand)]"
      />
      <span>
        <span className="block text-[13px] font-medium text-ink">{title}</span>
        <span className="mt-0.5 block text-[12px] leading-relaxed text-muted">{body}</span>
      </span>
    </label>
  );
}

function Minted({
  connection,
  header,
  onDone,
}: {
  connection: Connection;
  header: string;
  onDone: () => void;
}) {
  const token = connection.token ?? "";
  const isBuyer = connection.scope === "buyer";
  const endpoint = isBuyer
    ? (connection.endpoints.quote ?? "")
    : (connection.endpoints.verdicts ?? "");

  const curl = isBuyer
    ? `curl -s -X POST ${connection.endpoints.quote} \\
  -H "Content-Type: application/json" \\
  -H "${header}: ${token}" \\
  -d '{"items":[{"sku":"DWP-TEA-001","quantity":2}]}'`
    : `curl -s ${connection.endpoints.verdicts} \\
  -H "${header}: ${token}"`;

  return (
    <div className="space-y-5 px-4 py-5 sm:px-5">
      <div className="rounded-[10px] border border-escalate/30 bg-escalate-bg px-4 py-3">
        <p className="text-[13px] font-medium text-escalate">
          Copy this token now. It will not be shown again.
        </p>
        <p className="mt-1 text-[12px] leading-relaxed text-body">
          Only its SHA-256 digest is stored, so a leaked database row cannot be replayed as a
          token, and neither can this page.
        </p>
      </div>

      <CopyField label="Token" value={token} hint={connection.label} />
      <CopyField label="Header" value={header} mono />
      <CopyField label="Endpoint" value={endpoint} />

      <CopyBlock label="Try it" value={curl} />

      <div className="rounded-[10px] border border-line bg-sunken px-4 py-3 text-[12px] leading-relaxed text-muted">
        This connection is scoped to{" "}
        <span className="font-medium text-ink">{connection.scope}</span> and its agent identifier
        is <span className="font-mono text-[11.5px] text-ink">{connection.agent_id}</span>. The
        token identifies your agent and routes its notifications; it grants no purchasing
        authority, which comes from the mandates a human signed and from nowhere else.
      </div>

      <button
        type="button"
        onClick={onDone}
        className="rounded-[9px] border border-line-strong bg-surface px-4 py-2.5 text-[13px] font-medium text-ink transition-colors hover:bg-sunken"
      >
        I have copied it
      </button>
    </div>
  );
}

export function RevokeConnection({ connectionId }: { connectionId: string }) {
  const [pending, start] = useTransition();
  const [done, setDone] = useState(false);

  if (done) return <span className="text-[12px] text-muted">revoked</span>;

  return (
    <button
      type="button"
      disabled={pending}
      onClick={() =>
        start(async () => {
          const result = await revokeConnection(connectionId);
          if (result.ok) setDone(true);
        })
      }
      className="rounded-[7px] border border-line px-2.5 py-1 text-[11px] font-medium text-deny transition-colors hover:bg-deny-bg disabled:opacity-60"
    >
      {pending ? "Revoking..." : "Revoke"}
    </button>
  );
}
