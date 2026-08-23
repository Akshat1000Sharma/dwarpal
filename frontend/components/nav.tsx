"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/traffic", label: "Agent traffic" },
  { href: "/verdicts", label: "Verdict log" },
  { href: "/mandates", label: "Mandates" },
  { href: "/agents", label: "Agent controls" },
  { href: "/evidence", label: "Evidence" },
  { href: "/disputes", label: "Disputes" },
  { href: "/scorecards", label: "Scorecards" },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="scroll-x border-b border-line bg-surface">
      <ul className="flex min-w-max gap-1 px-4">
        {LINKS.map((link) => {
          const active =
            link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
          return (
            <li key={link.href}>
              <Link
                href={link.href}
                className={`inline-block border-b-2 px-3 py-3 text-sm transition-colors ${
                  active
                    ? "border-accent font-medium text-foreground"
                    : "border-transparent text-muted hover:text-foreground"
                }`}
              >
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
