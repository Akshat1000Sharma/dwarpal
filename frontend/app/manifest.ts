import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Dwarpal merchant dashboard",
    short_name: "Dwarpal",
    description:
      "The merchant's view of agent traffic, policy verdicts, mandates, evidence and disputes.",
    start_url: "/",
    display: "standalone",
    background_color: "#071121",
    theme_color: "#071121",
    icons: [
      { src: "/icon-192x192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512x512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
