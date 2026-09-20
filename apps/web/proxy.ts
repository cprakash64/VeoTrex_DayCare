import { NextResponse, type NextRequest } from "next/server";

import { MISCONFIGURED_MESSAGE, MISCONFIGURED_STATUS, loginIsBlocked } from "./lib/auth-guard";
import { auth0 } from "./lib/auth0";

export async function proxy(request: NextRequest) {
  // Fail closed before Auth0 is contacted. A login started without an organization yields
  // a token with no org_id, which the API rejects on every call - an authenticated-looking
  // session that cannot do anything is worse than a clear refusal here.
  if (loginIsBlocked(request.nextUrl.pathname, process.env)) {
    return new NextResponse(MISCONFIGURED_MESSAGE, {
      status: MISCONFIGURED_STATUS,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    });
  }
  return auth0.middleware(request);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)"],
};
