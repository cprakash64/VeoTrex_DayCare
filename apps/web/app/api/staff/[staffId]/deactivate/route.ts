import { NextRequest, NextResponse } from "next/server";

import { UUID_PATTERN } from "../../../../../lib/ring-inventory";
import { isSameOriginPost } from "../../../../../lib/ring-link";
import { setStaffActive } from "../../../../../lib/veotrex-api";

export async function POST(request: NextRequest, { params }: { params: Promise<{ staffId: string }> }) {
  const { staffId } = await params;
  if (!isSameOriginPost(request.url, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (!UUID_PATTERN.test(staffId)) return NextResponse.json({ status: "failed" }, { status: 422 });
  try {
    const response = await setStaffActive(staffId, false);
    return NextResponse.json({ status: response.ok ? "updated" : "failed" }, { status: response.status });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
