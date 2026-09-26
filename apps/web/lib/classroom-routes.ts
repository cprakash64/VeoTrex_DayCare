import { NextRequest, NextResponse } from "next/server";

import { UUID_PATTERN } from "./ring-inventory";
import { isSameOriginPost } from "./ring-link";

/**
 * Shared guards for the classroom BFF routes (V1-04A): same-origin mutation, JSON bodies only,
 * UUID path segments, and a relay that passes back the API's bounded error category and nothing
 * else from its body.
 */
export function refuse(status: number, category?: string): NextResponse {
  return NextResponse.json({ status: "failed", category: category ?? null }, { status });
}

export function guardMutation(request: NextRequest, ...ids: string[]): NextResponse | null {
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) return refuse(403);
  if (ids.some((id) => !UUID_PATTERN.test(id))) return refuse(422);
  return null;
}

export async function readJson(request: NextRequest): Promise<unknown | NextResponse> {
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") return refuse(415);
  try {
    return (await request.json()) as unknown;
  } catch {
    return refuse(400);
  }
}

const CATEGORY = /^[a-z_]{1,64}$/;

export async function relay(response: Response, success: string): Promise<NextResponse> {
  if (response.ok) {
    const body = (await response.json()) as { classroom_id?: unknown };
    return NextResponse.json(
      {
        status: success,
        classroom_id: typeof body.classroom_id === "string" ? body.classroom_id : null,
      },
      { status: response.status },
    );
  }
  let category: string | undefined;
  try {
    const body = (await response.json()) as { detail?: { category?: unknown } };
    const value = body.detail?.category;
    if (typeof value === "string" && CATEGORY.test(value)) category = value;
  } catch {
    category = undefined;
  }
  return refuse(response.status, category);
}
