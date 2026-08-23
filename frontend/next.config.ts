import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The Dockerfile deploys the traced standalone output rather than the whole node_modules tree.
  output: "standalone",
};

export default nextConfig;
