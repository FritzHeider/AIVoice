import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // `make lint` runs ESLint with the shared flat config; `next build` must not run its own pass.
  eslint: { ignoreDuringBuilds: true },
};

export default config;
