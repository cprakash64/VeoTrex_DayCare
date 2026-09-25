import { NextRequest, NextResponse } from "next/server";

import { UUID_PATTERN } from "../../../../lib/ring-inventory";
import { isSameOriginPost } from "../../../../lib/ring-link";
import { deleteStaff, renameStaff } from "../../../../lib/veotrex-api";

type Context = { params: Promise<{ staffId: string }> };

function guard(request: NextRequest, staffId: string): NextResponse | null {
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (!UUID_PATTERN.test(staffId)) return NextResponse.json({ status: "failed" }, { status: 422 });
  return null;
}

export async function PATCH(request: NextRequest, { params }: Context) {
  const { staffId } = await params;
  const refused = guard(request, staffId);
  if (refused) return refused;
  let body: { display_name?: unknown };
  try {
    body = (await request.json()) as { display_name?: unknown };
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 400 });
  }
  const name = typeof body.display_name === "string" ? body.display_name.trim() : "";
  if (!name || name.length > 200) return NextResponse.json({ status: "failed" }, { status: 422 });
  try {
    const response = await renameStaff(staffId, name);
    return NextResponse.json({ status: response.ok ? "renamed" : "failed" }, { status: response.status });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}

export async function DELETE(request: NextRequest, { params }: Context) {
  const { staffId } = await params;
  const refused = guard(request, staffId);
  if (refused) return refused;
  try {
    const response = await deleteStaff(staffId);
    return NextResponse.json({ status: response.ok ? "deleted" : "failed" }, { status: response.status });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
