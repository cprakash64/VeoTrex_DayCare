import { NextRequest, NextResponse } from "next/server";

import { isSameOriginPost } from "../../../lib/ring-link";
import { createStaff } from "../../../lib/veotrex-api";

export async function POST(request: NextRequest) {
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
    return NextResponse.json({ status: "failed" }, { status: 415 });
  }
  let body: { display_name?: unknown };
  try {
    body = (await request.json()) as { display_name?: unknown };
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 400 });
  }
  const name = typeof body.display_name === "string" ? body.display_name.trim() : "";
  if (!name || name.length > 200 || Object.keys(body).some((key) => key !== "display_name")) {
    return NextResponse.json({ status: "failed" }, { status: 422 });
  }
  try {
    const response = await createStaff(name);
    if (!response.ok) return NextResponse.json({ status: "failed" }, { status: response.status });
    const created = (await response.json()) as { staff_id?: unknown };
    return NextResponse.json(
      { status: "created", staff_id: typeof created.staff_id === "string" ? created.staff_id : null },
      { status: 201 },
    );
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
