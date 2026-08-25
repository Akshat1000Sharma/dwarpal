import { cookies } from "next/headers";
import Link from "next/link";
import type { ReactNode } from "react";

import { PERSONA_COOKIE } from "@/app/login/persona";
import { type Persona, Sidebar } from "@/components/sidebar";
import { backendReachable } from "@/lib/backend";

/**
 * The shell both consoles share: one sidebar, one slim status strip, and the page.
 *
 * The persona comes from the cookie the login page sets. It decides which navigation is shown and
 * nothing else; every route remains reachable by URL, because the cookie is a preference, not a
 * permission, and dressing it up as one would be dishonest.
 */
export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  const jar = await cookies();
  const stored = jar.get(PERSONA_COOKIE)?.value;
  const persona: Persona = stored === "buyer" ? "buyer" : "merchant";
  const reachable = await backendReachable();

  return (
    <div className="flex min-h-dvh flex-col md:flex-row">
      <Sidebar persona={persona} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="hidden h-[60px] shrink-0 items-center justify-between gap-4 border-b border-line bg-surface px-6 md:flex">
          <div className="flex items-center gap-2.5 text-[13px] text-muted">
            <span
              className={`h-1.5 w-1.5 rounded-full ${reachable ? "bg-allow" : "bg-deny"}`}
              aria-hidden="true"
            />
            <span>{reachable ? "Merchant reachable" : "Merchant unreachable"}</span>
          </div>
          <div className="flex items-center gap-5 text-[12px] text-faint">
            <span>Designed for UAP, compliant with AP2</span>
            <Link href="/" className="text-muted transition-colors hover:text-ink">
              Back to the site
            </Link>
          </div>
        </div>

        <main className="flex-1 px-4 py-6 sm:px-6 sm:py-8">
          <div className="mx-auto w-full max-w-[1180px] space-y-6">{children}</div>
        </main>

        <footer className="border-t border-line px-4 py-5 text-[12px] leading-relaxed text-faint sm:px-6">
          Every money decision on these pages was made by the deterministic policy kernel. No model
          is consulted on that path, and the guard that keeps it that way is a test, not a
          convention.
        </footer>
      </div>
    </div>
  );
}
