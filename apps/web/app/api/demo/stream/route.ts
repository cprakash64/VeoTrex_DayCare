import { auth0 } from "../../../../lib/auth0";
import { demoRuntimeBaseUrl } from "../../../../lib/demo-runtime";

export const dynamic = "force-dynamic";

/**
 * Authenticated MJPEG proxy.
 *
 * The upstream body is passed through as a stream rather than buffered, so memory does not
 * grow with the length of the recording. An unauthenticated caller gets 401 and no imagery.
 */
export async function GET(): Promise<Response> {
  const session = await auth0.getSession();
  if (!session) {
    return new Response("unauthenticated", { status: 401 });
  }
  const baseUrl = demoRuntimeBaseUrl();
  if (!baseUrl) {
    return new Response("monitoring runtime is not configured", { status: 503 });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${baseUrl}/stream.mjpg`, { cache: "no-store" });
  } catch {
    return new Response("monitoring runtime is unreachable", { status: 503 });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response("monitoring runtime returned no stream", { status: 503 });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type":
        upstream.headers.get("Content-Type") ?? "multipart/x-mixed-replace; boundary=veotrexframe",
      "Cache-Control": "no-store, no-cache, must-revalidate",
    },
  });
}
