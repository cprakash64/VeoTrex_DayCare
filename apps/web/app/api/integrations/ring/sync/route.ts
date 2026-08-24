import { NextRequest, NextResponse } from "next/server";

import { syncRingConnection } from "../../../../../lib/veotrex-api";
import { UUID_PATTERN } from "../../../../../lib/ring-inventory";
import { isSameOriginPost } from "../../../../../lib/ring-link";

export async function POST(request: NextRequest) {
  if (!isSameOriginPost(request.url, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
    return NextResponse.json({ status: "failed" }, { status: 415 });
  }
  let body: { connection_id?: unknown };
  try {
    body = (await request.json()) as { connection_id?: unknown };
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 400 });
  }
  if (
    typeof body.connection_id !== "string" ||
    !UUID_PATTERN.test(body.connection_id) ||
    Object.keys(body).some((key) => key !== "connection_id")
  ) {
    return NextResponse.json({ status: "failed" }, { status: 422 });
  }
  try {
    const response = await syncRingConnection(body.connection_id);
    return NextResponse.json(
      { status: response.ok ? "synchronized" : "failed" },
      { status: response.status },
    );
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
