import type { Metadata } from "next";
import Image from "next/image";
import type { ReactNode } from "react";
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

const SITE_URL = process.env.SITE_URL ?? "http://localhost:3000";
const DESCRIPTION =
  "The merchant's view of agent traffic, policy verdicts, mandates, evidence and disputes.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "Dwarpal merchant dashboard",
  description: DESCRIPTION,
  applicationName: "Dwarpal",
  icons: {
    // app/favicon.ico is picked up by the file convention; these are the explicit sizes.
    icon: [
      { url: "/favicon.png", type: "image/png", sizes: "32x32" },
      { url: "/icon-192x192.png", type: "image/png", sizes: "192x192" },
      { url: "/icon-512x512.png", type: "image/png", sizes: "512x512" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180" }],
  },
  openGraph: {
    type: "website",
    siteName: "Dwarpal",
    title: "Dwarpal merchant dashboard",
    description: DESCRIPTION,
    url: "/",
    images: [{ url: "/og-image.png", width: 1200, height: 630, alt: "Dwarpal" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Dwarpal merchant dashboard",
    description: DESCRIPTION,
    images: ["/og-image.png"],
  },
};

/**
 * The children type is written out rather than using the generated LayoutProps helper, for the
 * same reason as the route handler: that helper only exists once a build has emitted .next/types,
 * and the type check runs before the build.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col font-sans">
        <header className="border-b border-line bg-surface">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-4">
            <div className="flex items-center gap-3">
              <Image
                src="/icon-192x192.png"
                alt=""
                width={36}
                height={36}
                className="rounded"
                priority
              />
              <div>
              <h1 className="text-base font-semibold tracking-tight">Dwarpal</h1>
              <p className="text-xs text-muted">
                The AP2 merchant endpoint for Razorpay. Agents never touch this dashboard.
              </p>
              </div>
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
