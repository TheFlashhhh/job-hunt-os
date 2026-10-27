import type { NextConfig } from "next";

/**
 * The backend origin, used only by SERVER-side rendering inside this Node
 * process. The browser never sees it.
 */
const BACKEND_ORIGIN =
  process.env.JOBHUNT_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  /**
   * Same-origin proxy for every browser-initiated API call.
   *
   * Server components fetch the backend directly from Node, which is why the
   * page rendered fine while client actions failed with "Failed to fetch":
   * a browser POST from http://localhost:3000 to http://127.0.0.1:8000 is
   * cross-origin (different host AND port), so Chrome sent a CORS preflight
   * first — and FastAPI had no CORS middleware, answering OPTIONS with 405.
   *
   * Proxying through Next removes the cross-origin condition entirely rather
   * than negotiating it, so no preflight is ever issued and the browser does
   * not depend on a NEXT_PUBLIC_* backend URL being correct.
   */
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${BACKEND_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
