import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
  display: "swap",
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  display: "swap",
});

const SITE_URL = process.env.SITE_URL ?? "http://localhost:3000";
const TITLE = "Dwarpal";
const TAGLINE = "The AP2 merchant endpoint for Razorpay";
const DESCRIPTION =
  "An AI agent can now spend a person's money. Dwarpal is the gate that decides whether it was " +
  "allowed to, and the evidence that proves it afterwards.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: `${TITLE} - ${TAGLINE}`, template: `%s - ${TITLE}` },
  description: DESCRIPTION,
  applicationName: TITLE,
  icons: {
    // app/favicon.ico is picked up by the file convention; these are the explicit sizes.
    icon: [
      { url: "/dwarpal-mark-32.png", type: "image/png", sizes: "32x32" },
      { url: "/dwarpal-mark-192.png", type: "image/png", sizes: "192x192" },
      { url: "/dwarpal-mark-512.png", type: "image/png", sizes: "512x512" },
    ],
    apple: [{ url: "/dwarpal-apple-touch-icon.png", sizes: "180x180" }],
  },
  openGraph: {
    type: "website",
    siteName: TITLE,
    title: `${TITLE} - ${TAGLINE}`,
    description: DESCRIPTION,
    url: "/",
    images: [{ url: "/dwarpal-og-card.png", width: 1200, height: 630, alt: TITLE }],
  },
  twitter: {
    card: "summary_large_image",
    title: `${TITLE} - ${TAGLINE}`,
    description: DESCRIPTION,
    images: ["/dwarpal-og-card.png"],
  },
};

export const viewport: Viewport = {
  themeColor: "#ffffff",
  width: "device-width",
  initialScale: 1,
};

/**
 * The root layout carries no chrome. The landing page and the login page are full-bleed, and the
 * consoles bring their own shell from app/(console)/layout.tsx.
 *
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
      <body className="min-h-full font-sans">{children}</body>
    </html>
  );
}
