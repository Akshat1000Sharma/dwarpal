import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The Dockerfile deploys the traced standalone output rather than the whole node_modules tree.
  output: "standalone",

  images: {
    // The seeded catalog images are vendored under public/catalog, so the common case needs no
    // remote host at all. Hotlinking Wikimedia was measured at 64 of 122 optimizer fetches
    // succeeding and the rest rate limited, which is fine for a wiki and useless for a shop.
    // The pattern stays because the seed accepts an absolute image_url, and next/image refuses any
    // host not named here: an image pointed somewhere else fails loudly instead of rendering blank.
    remotePatterns: [
      { protocol: "https", hostname: "upload.wikimedia.org", pathname: "/wikipedia/**" },
    ],
    minimumCacheTTL: 60 * 60 * 24 * 30,
  },
};

export default nextConfig;
