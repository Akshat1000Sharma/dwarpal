import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";

import { chooseProfile } from "./actions";

export const metadata: Metadata = { title: "Choose a view" };

/**
 * Two doors, no passwords.
 *
 * The page says so plainly. Pretending to authenticate would be the sort of claim this project
 * spends the rest of its documentation refusing to make.
 */
export default function LoginPage() {
  async function asMerchant() {
    "use server";
    await chooseProfile("merchant");
  }

  async function asBuyer() {
    "use server";
    await chooseProfile("buyer");
  }

  return (
    <main className="relative flex min-h-dvh flex-col items-center justify-center px-5 py-14">
      <div className="pointer-events-none absolute inset-0 grid-field" aria-hidden="true" />

      <div className="relative w-full max-w-[880px]">
        <Link href="/" className="mb-10 flex items-center justify-center gap-2.5">
          <Image src="/dwarpal-mark-192.png" alt="" width={40} height={40} priority />
          <span className="text-[19px] font-semibold tracking-tight text-ink">Dwarpal</span>
        </Link>

        <div className="text-center">
          <h1 className="display text-[32px] sm:text-[40px]">Which side of the counter?</h1>
          <p className="mx-auto mt-3 max-w-[46ch] text-[14px] leading-relaxed text-muted">
            Both consoles run against the same merchant and the same policy kernel. Pick the one
            you want to look through.
          </p>
        </div>

        <div className="mt-10 grid gap-4 sm:grid-cols-2">
          <Door
            action={asBuyer}
            eyebrow="Buyer"
            title="Send an agent shopping"
            cta="Continue as a buyer"
            points={[
              "Ask an AI agent to buy something in plain language",
              "Watch every step it takes, in its own log",
              "Pay with the Razorpay test card and nothing else",
            ]}
          />
          <Door
            action={asMerchant}
            eyebrow="Merchant"
            title="Run the gate"
            cta="Continue as the merchant"
            points={[
              "Every policy decision, with the reason code behind it",
              "Mandates, agent limits and a per-agent kill switch",
              "Hash-chained evidence, and the disputes it defends",
            ]}
            primary
          />
        </div>

        <p className="mt-8 text-center text-[12px] leading-relaxed text-faint">
          There is no password here, and this is not authentication. It sets a cookie that decides
          which navigation you see. Dwarpal&apos;s real boundaries are the credential chain an
          agent presents and the merchant token the dashboard sends from the server.
        </p>
      </div>
    </main>
  );
}

function Door({
  action,
  eyebrow,
  title,
  points,
  cta,
  primary = false,
}: {
  action: () => Promise<void>;
  eyebrow: string;
  title: string;
  points: string[];
  cta: string;
  primary?: boolean;
}) {
  return (
    <form action={action} className="h-full">
      <div className="flex h-full flex-col rounded-[16px] border border-line bg-surface p-6 shadow-e1 transition-shadow duration-300 hover:shadow-e2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.09em] text-faint">
          {eyebrow}
        </div>
        <h2 className="mt-2 text-[20px] font-semibold tracking-[-0.015em] text-ink">{title}</h2>
        <ul className="mt-4 flex-1 space-y-2.5">
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
        <button
          type="submit"
          className={`mt-6 w-full rounded-[9px] px-4 py-2.5 text-[13px] font-medium transition-colors ${
            primary
              ? "bg-brand text-white hover:bg-brand-strong"
              : "border border-line-strong bg-surface text-ink hover:bg-sunken"
          }`}
        >
          {cta}
        </button>
      </div>
    </form>
  );
}
