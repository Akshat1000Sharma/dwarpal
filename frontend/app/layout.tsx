import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Nav } from "@/components/nav";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Dwarpal merchant dashboard",
  description:
    "The merchant's view of agent traffic, policy verdicts, mandates, evidence and disputes.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col font-sans">
        <header className="border-b border-line bg-surface">
          <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-4">
            <div>
              <h1 className="text-base font-semibold tracking-tight">Dwarpal</h1>
              <p className="text-xs text-muted">
                The AP2 merchant endpoint for Razorpay. Agents never touch this dashboard.
              </p>
            </div>
            <p className="text-xs text-muted">
              Designed for UAP, compliant with AP2
            </p>
          </div>
        </header>
        <Nav />
        <main className="flex-1 px-4 py-6">
          <div className="mx-auto w-full max-w-7xl space-y-6">{children}</div>
        </main>
        <footer className="border-t border-line px-4 py-4 text-xs text-muted">
          Every money decision on this page was made by the deterministic policy kernel. No model
          is consulted on that path.
        </footer>
      </body>
    </html>
  );
}
