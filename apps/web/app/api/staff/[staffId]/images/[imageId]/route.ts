import { NextRequest, NextResponse } from "next/server";

import { auth0 } from "../../../../../../lib/auth0";
import { UUID_PATTERN } from "../../../../../../lib/ring-inventory";
import { isSameOriginPost } from "../../../../../../lib/ring-link";
import { deleteStaffImage, getStaffImageContent } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ staffId: string; imageId: string }> };

/** Thumbnail bytes for the signed-in tenant member. Private, never cached, never a path. */
export async function GET(_request: NextRequest, { params }: Context) {
  const { staffId, imageId } = await params;
  if (!UUID_PATTERN.test(staffId) || !UUID_PATTERN.test(imageId)) {
    return new NextResponse(null, { status: 404 });
  }
  const session = await auth0.getSession();
  if (!session?.user) return new NextResponse(null, { status: 401 });
  try {
    const response = await getStaffImageContent(staffId, imageId);
    if (!response.ok) return new NextResponse(null, { status: response.status === 404 ? 404 : 502 });
    return new NextResponse(await response.arrayBuffer(), {
      status: 200,
      headers: {
        "content-type": "image/jpeg",
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
        "content-disposition": "inline",
      },
    });
  } catch {
    return new NextResponse(null, { status: 503 });
  }
}

export async function DELETE(request: NextRequest, { params }: Context) {
  const { staffId, imageId } = await params;
  if (!isSameOriginPost(request.url, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (!UUID_PATTERN.test(staffId) || !UUID_PATTERN.test(imageId)) {
    return NextResponse.json({ status: "failed" }, { status: 422 });
  }
  try {
    const response = await deleteStaffImage(staffId, imageId);
    return NextResponse.json({ status: response.ok ? "removed" : "failed" }, { status: response.status });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
