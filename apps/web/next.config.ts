import type { NextConfig } from "next";

/**
 * Standalone output lets the staging image ship the server plus only the traced
 * dependencies, instead of the full pnpm store. Without it a production container has to
 * carry node_modules, which is both larger and harder to reason about.
 *
 * `poweredByHeader` is disabled so the public origin does not advertise its framework.
 * The file-tracing root is left to Next's own workspace detection; pinning it with
 * __dirname is unreliable because a TypeScript config may be loaded as an ES module.
 */
const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
};

export default nextConfig;
