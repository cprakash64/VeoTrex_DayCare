import { auth0 } from "../../../../lib/auth0";
import { demoRuntimeBaseUrl, fetchMonitoringStatus } from "../../../../lib/demo-runtime";

export const dynamic = "force-dynamic";

/** Authenticated read of the device-local monitoring state. */
export async function GET(): Promise<Response> {
  const session = await auth0.getSession();
  if (!session) {
    return Response.json({ error: "unauthenticated" }, { status: 401 });
  }
  if (!demoRuntimeBaseUrl()) {
    return Response.json({ available: false, reason: "not_configured" }, { status: 200 });
  }
  const status = await fetchMonitoringStatus();
  return Response.json(status, { status: 200, headers: { "Cache-Control": "no-store" } });
}
