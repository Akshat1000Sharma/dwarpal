"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/**
 * The single navigation surface.
 *
 * It replaces the header row and the tab strip that used to sit above every page. Three shapes,
 * one component:
 *
 *   >= 1024px   a fixed 264px rail
 *   768-1023px  a 72px icon rail, labels in the title attribute
 *   < 768px     hidden, opened as a drawer, focus trapped while it is open
 */

export type Persona = "merchant" | "buyer";

type Item = { href: string; label: string; icon: Icon; hint?: string };
type Group = { heading: string; items: Item[] };

type Icon =
  | "overview"
  | "traffic"
  | "verdicts"
  | "mandates"
  | "agents"
  | "evidence"
  | "disputes"
  | "scorecards"
  | "connect"
  | "buy"
  | "runs"
  | "catalog"
  | "setup";

const MERCHANT: Group[] = [
  {
    heading: "Watch",
    items: [
      { href: "/merchant", label: "Overview", icon: "overview" },
      { href: "/merchant/traffic", label: "Agent traffic", icon: "traffic" },
      { href: "/merchant/verdicts", label: "Verdict log", icon: "verdicts" },
    ],
  },
  {
    heading: "Control",
    items: [
      { href: "/merchant/mandates", label: "Mandates", icon: "mandates" },
      { href: "/merchant/agents", label: "Agent controls", icon: "agents" },
      { href: "/merchant/connections", label: "Connect an agent", icon: "connect" },
    ],
  },
  {
    heading: "Prove",
    items: [
      { href: "/merchant/evidence", label: "Evidence", icon: "evidence" },
      { href: "/merchant/disputes", label: "Disputes", icon: "disputes" },
      { href: "/merchant/scorecards", label: "Scorecards", icon: "scorecards" },
    ],
  },
];

const BUYER: Group[] = [
  {
    heading: "Shop",
    items: [
      { href: "/buyer", label: "Send an agent", icon: "buy" },
      { href: "/buyer/runs", label: "Agent runs", icon: "runs" },
      { href: "/buyer/catalog", label: "Catalog", icon: "catalog" },
    ],
  },
  {
    heading: "Set up",
    items: [{ href: "/buyer/setup", label: "Configure your agent", icon: "setup" }],
  },
];

export function groupsFor(persona: Persona): Group[] {
  return persona === "buyer" ? BUYER : MERCHANT;
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/merchant" || href === "/buyer") return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Sidebar({ persona }: { persona: Persona }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const groups = groupsFor(persona);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        opener.current?.focus();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      // Trap the focus while the drawer covers the page, so tabbing cannot walk behind it.
      const focusable = panel.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open]);

  return (
    <>
      {/* Mobile bar. Below md this is the only chrome. */}
      <div className="sticky top-0 z-40 flex items-center gap-3 border-b border-line bg-surface/95 px-4 py-3 backdrop-blur md:hidden">
        <button
          ref={opener}
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open navigation"
          aria-expanded={open}
          className="grid h-9 w-9 place-items-center rounded-[8px] border border-line text-ink transition-colors hover:bg-sunken"
        >
          <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
            <path
              d="M3 6h14M3 10h14M3 14h14"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </button>
        <Link href="/" onClick={() => setOpen(false)} className="flex items-center gap-2">
          <Mark />
          <span className="text-[15px] font-semibold tracking-tight text-ink">Dwarpal</span>
        </Link>
        <span className="ml-auto text-[11px] uppercase tracking-[0.06em] text-faint">
          {persona}
        </span>
      </div>

      {open && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-40 bg-[color:var(--overlay)] md:hidden"
        />
      )}

      <div
        ref={panel}
        data-open={open}
        className="nav-drawer fixed inset-y-0 left-0 z-50 flex w-[264px] flex-col border-r border-line bg-surface md:sticky md:top-0 md:z-30 md:h-dvh md:w-[72px] lg:w-[264px]"
      >
        <div className="flex h-[60px] shrink-0 items-center gap-2.5 border-b border-line px-4 md:justify-center lg:justify-start">
          <Link
            href="/"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5"
            aria-label="Dwarpal home"
          >
            <Mark />
            <span className="text-[15px] font-semibold tracking-tight text-ink md:hidden lg:inline">
              Dwarpal
            </span>
          </Link>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4 md:px-2 lg:px-3" aria-label="Main">
          {groups.map((group) => (
            <div key={group.heading} className="mb-5 last:mb-0">
              <div className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-[0.09em] text-faint md:hidden lg:block">
                {group.heading}
              </div>
              <ul className="space-y-0.5">
                {group.items.map((item) => {
                  const active = isActive(pathname, item.href);
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        title={item.label}
                        onClick={() => setOpen(false)}
                        aria-current={active ? "page" : undefined}
                        className={`flex items-center gap-2.5 rounded-[8px] px-2.5 py-2 text-[13px] transition-colors md:justify-center lg:justify-start ${
                          active
                            ? "bg-brand-tint font-medium text-brand-strong"
                            : "text-body hover:bg-sunken hover:text-ink"
                        }`}
                      >
                        <NavIcon name={item.icon} />
                        <span className="md:hidden lg:inline">{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="shrink-0 border-t border-line p-3 md:px-2 lg:px-3">
          <Link
            href="/login"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 rounded-[8px] px-2.5 py-2 text-[13px] text-body transition-colors hover:bg-sunken hover:text-ink md:justify-center lg:justify-start"
            title="Switch view"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" aria-hidden="true">
              <path
                d="M7 4H4v12h3M13 7l3 3-3 3M16 10H8"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span className="md:hidden lg:inline">
              Switch view
              <span className="ml-1.5 text-faint">({persona})</span>
            </span>
          </Link>
        </div>
      </div>
    </>
  );
}

function Mark() {
  return (
    <Image
      src="/icon-192x192.png"
      alt=""
      width={28}
      height={28}
      className="rounded-[7px]"
      priority
    />
  );
}

const PATHS: Record<Icon, string> = {
  overview: "M3 10h5V3H3zM12 17h5V3h-5zM3 17h5v-4H3z",
  traffic: "M3 16V9m5 7V4m5 12v-5m5 5V7",
  verdicts: "M4 4h12M4 8h12M4 12h8M4 16h5",
  mandates: "M5 3h7l3 3v11H5zM12 3v3h3",
  agents: "M10 10a3 3 0 100-6 3 3 0 000 6zM4 17c0-3 3-5 6-5s6 2 6 5",
  evidence: "M6 3h8v14l-4-2-4 2zM8 7h4M8 10h4",
  disputes: "M10 3l7 13H3zM10 8v4M10 14h.01",
  scorecards: "M4 16V8m4 8V4m4 12v-6m4 6V6",
  connect: "M8 12l-2 2a3 3 0 11-4-4l2-2M12 8l2-2a3 3 0 114 4l-2 2M7 13l6-6",
  buy: "M3 4h2l2 9h8l2-6H6M8 17h.01M15 17h.01",
  runs: "M10 3v7l4 2M10 17a7 7 0 110-14 7 7 0 010 14z",
  catalog: "M3 5h6v6H3zM11 5h6v6h-6zM3 13h6v4H3zM11 13h6v4h-6z",
  setup: "M10 13a3 3 0 100-6 3 3 0 000 6zM10 2v2M10 16v2M2 10h2M16 10h2M4.5 4.5l1.4 1.4M14.1 14.1l1.4 1.4M15.5 4.5l-1.4 1.4M5.9 14.1l-1.4 1.4",
};

function NavIcon({ name }: { name: Icon }) {
  return (
    <svg viewBox="0 0 20 20" className="h-[17px] w-[17px] shrink-0" aria-hidden="true">
      <path
        d={PATHS[name]}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
