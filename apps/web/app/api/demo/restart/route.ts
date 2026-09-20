import { auth0 } from "../../../../lib/auth0";
import { demoRuntimeBaseUrl } from "../../../../lib/demo-runtime";

export const dynamic = "force-dynamic";

/** Operator control: replay the recorded clip from its start. */
export async function POST(): Promise<Response> {
  const session = await auth0.getSession();
  if (!session) {
    return Response.json({ error: "unauthenticated" }, { status: 401 });
  }
  const baseUrl = demoRuntimeBaseUrl();
  if (!baseUrl) {
    return Response.json({ error: "not_configured" }, { status: 503 });
  }
  try {
    const upstream = await fetch(`${baseUrl}/control/restart`, {
      method: "POST",
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    if (!upstream.ok) {
      return Response.json({ error: "restart_failed" }, { status: 502 });
    }
  } catch {
    return Response.json({ error: "unreachable" }, { status: 503 });
  }
  return Response.json({ status: "restarted" });
}
