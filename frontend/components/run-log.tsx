"use client";

import Link from "next/link";
import Script from "next/script";
import { useCallback, useEffect, useRef, useState } from "react";

import type { BuyerRunDetail, BuyerRunEvent, GatewayMode } from "@/lib/types";

/**
 * The agent's log, live.
 *
 * It polls rather than streams. A run takes a couple of seconds and produces about ten events, so
 * a socket would be more machinery than the problem deserves, and polling degrades to a page
 * refresh if anything goes wrong with it. Polling stops the moment the run reaches a terminal
 * state, so an idle tab is not making requests forever.
 */

// Every BuyerRunStatus that carries a finished_at. awaiting_payment is deliberately absent:
// that run is still waiting for the operator to pay it, so it keeps polling.
const TERMINAL = new Set(["completed", "refused", "escalated", "compensated", "error"]);
const POLL_MS = 1500;

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => { open: () => void };
  }
}

export function RunLog({
  initial,
  gateway,
}: {
  initial: BuyerRunDetail;
  gateway: GatewayMode | null;
}) {
  const [run, setRun] = useState(initial);
  const [paying, setPaying] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const [checkoutReady, setCheckoutReady] = useState(false);
  const timer = useRef<number | null>(null);

  const settled = TERMINAL.has(run.status);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`/api/dwarpal/buyer/runs/${initial.id}`, {
        cache: "no-store",
      });
      if (!response.ok) return;
      setRun((await response.json()) as BuyerRunDetail);
    } catch {
      // A dropped poll is not worth surfacing; the next one will pick it up.
    }
  }, [initial.id]);

  useEffect(() => {
    if (settled) {
      if (timer.current) window.clearInterval(timer.current);
      return;
    }
    timer.current = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [settled, refresh]);

  const pay = () => {
    setPayError(null);
    if (!gateway || gateway.mode !== "razorpay" || !gateway.key_id || !run.razorpay_order_id) {
      setPayError("This merchant is not configured for Razorpay Checkout.");
      return;
    }
    if (!window.Razorpay) {
      setPayError("Razorpay Checkout has not finished loading yet. Try again in a moment.");
      return;
    }
    setPaying(true);
    const checkout = new window.Razorpay({
      key: gateway.key_id,
      order_id: run.razorpay_order_id,
      amount: run.amount.amount,
      currency: run.amount.currency,
      name: gateway.merchant.name,
      description: run.prompt.slice(0, 120),
      // The handler result is untrusted. The server re-checks its HMAC before anything moves.
      handler: async (result: Record<string, string>) => {
        try {
          const response = await fetch(`/api/dwarpal/buyer/runs/${run.id}/pay`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              razorpay_order_id: result.razorpay_order_id,
              razorpay_payment_id: result.razorpay_payment_id,
              razorpay_signature: result.razorpay_signature,
            }),
          });
          if (!response.ok) {
            const body = (await response.json()) as { detail?: string };
            setPayError(body.detail ?? `The merchant refused the payment (HTTP ${response.status}).`);
          }
        } catch (error) {
          setPayError(String(error).slice(0, 200));
        } finally {
          setPaying(false);
          await refresh();
        }
      },
      modal: { ondismiss: () => setPaying(false) },
      theme: { color: "#2b6cff" },
    });
    checkout.open();
  };

  return (
    <>
      {gateway?.mode === "razorpay" && (
        <Script
          src="https://checkout.razorpay.com/v1/checkout.js"
          strategy="lazyOnload"
          onLoad={() => setCheckoutReady(true)}
        />
      )}

      {run.status === "awaiting_payment" && (
        <div className="rounded-[14px] border border-brand/30 bg-brand-tint p-5">
          <h2 className="text-[15px] font-semibold text-navy">
            Approved. The order is waiting to be paid.
          </h2>
          <p className="mt-1.5 max-w-[70ch] text-[13px] leading-relaxed text-body">
            The policy kernel has already decided. Nothing about paying can widen what it allowed,
            and the merchant re-checks the signed result before it captures anything.
          </p>

          {gateway?.mode === "razorpay" ? (
            <>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={pay}
                  disabled={paying}
                  className="rounded-[9px] bg-brand px-5 py-2.5 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong disabled:opacity-60"
                >
                  {paying ? "Waiting for Razorpay..." : "Pay with the test card"}
                </button>
                <span className="font-mono text-[12px] text-muted">
                  {gateway.test_card.number} / {gateway.test_card.expiry} / {gateway.test_card.cvv}
                </span>
              </div>
              {!checkoutReady && (
                <p className="mt-2 text-[11.5px] text-faint">Loading Razorpay Checkout...</p>
              )}
            </>
          ) : (
            <p className="mt-4 text-[13px] text-body">
              This merchant is running the stub gateway, so there is no hosted Checkout to open.
              The order id is{" "}
              <code className="font-mono text-[12px] text-ink">{run.razorpay_order_id}</code>.
            </p>
          )}

          {payError && <p className="mt-3 text-[12.5px] text-deny">{payError}</p>}
        </div>
      )}

      <div className="overflow-hidden rounded-[14px] border border-line bg-surface shadow-e1">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3.5 sm:px-5">
          <div className="flex items-center gap-2.5">
            <span
              className={`h-1.5 w-1.5 rounded-full ${settled ? "bg-muted" : "animate-pulse bg-brand"}`}
              aria-hidden="true"
            />
            <h2 className="text-[14px] font-semibold text-ink">
              {settled ? "Agent log" : "Agent log, live"}
            </h2>
          </div>
          <span className="font-mono text-[11.5px] text-faint">{run.agent_id}</span>
        </header>

        <ol className="divide-y divide-[color:var(--line)]">
          {run.events.map((event) => (
            <LogRow key={event.seq} event={event} correlationId={run.correlation_id} />
          ))}
          {run.events.length === 0 && (
            <li className="px-5 py-10 text-center text-[13px] text-muted">
              The agent is starting up...
            </li>
          )}
        </ol>
      </div>
    </>
  );
}

const LEVEL_DOT: Record<string, string> = {
  info: "bg-brand",
  warn: "bg-escalate",
  error: "bg-deny",
};

function LogRow({ event, correlationId }: { event: BuyerRunEvent; correlationId: string }) {
  const [open, setOpen] = useState(false);
  const hasData = Object.keys(event.data ?? {}).length > 0;

  return (
    <li className="px-4 py-3.5 sm:px-5">
      <div className="flex items-start gap-3">
        <span
          className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${LEVEL_DOT[event.level] ?? "bg-muted"}`}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-[11px] uppercase tracking-[0.05em] text-faint">
              {event.step}
            </span>
            {event.duration_ms !== null && (
              <span className="font-mono text-[11px] tabular-nums text-faint">
                {event.duration_ms}ms
              </span>
            )}
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-body">{event.message}</p>

          {event.step === "verdict" && (
            <Link
              href={`/merchant/evidence/${correlationId}`}
              className="mt-2 inline-block text-[12px] text-brand hover:underline"
            >
              Open the evidence packet for this transaction
            </Link>
          )}

          {hasData && (
            <>
              <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                className="mt-2 text-[11.5px] text-muted transition-colors hover:text-ink"
                aria-expanded={open}
              >
                {open ? "Hide the detail" : "Show the detail"}
              </button>
              {open && (
                <pre className="scroll-x mt-2 rounded-[8px] border border-line bg-sunken p-3 text-[11.5px] leading-relaxed text-body">
                  {JSON.stringify(event.data, null, 2)}
                </pre>
              )}
            </>
          )}
        </div>
      </div>
    </li>
  );
}
