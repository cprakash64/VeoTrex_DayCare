import { NextRequest, NextResponse } from "next/server";

import { UUID_PATTERN } from "../../../../../lib/ring-inventory";
import { isSameOriginPost } from "../../../../../lib/ring-link";
import { ACCEPTED_UPLOAD_TYPES, MAX_UPLOAD_BYTES } from "../../../../../lib/staff";
import { uploadStaffImage } from "../../../../../lib/veotrex-api";

/**
 * Multipart from the browser in, raw bounded bytes to the API out. The API re-validates the
 * bytes themselves; this route only bounds size and forwards the declared type.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ staffId: string }> }) {
  const { staffId } = await params;
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (!UUID_PATTERN.test(staffId)) return NextResponse.json({ status: "failed" }, { status: 422 });
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (!Number.isFinite(declared) || declared > MAX_UPLOAD_BYTES + 4096) {
    return NextResponse.json({ status: "rejected", category: "file_too_large" }, { status: 413 });
  }
  let file: File | null;
  try {
    const form = await request.formData();
    const entry = form.get("photo");
    file = entry instanceof File ? entry : null;
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 400 });
  }
  if (!file || file.size === 0) {
    return NextResponse.json({ status: "rejected", category: "empty_upload" }, { status: 422 });
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return NextResponse.json({ status: "rejected", category: "file_too_large" }, { status: 413 });
  }
  const mediaType = file.type.split(";", 1)[0].trim().toLowerCase();
  if (!(ACCEPTED_UPLOAD_TYPES as readonly string[]).includes(mediaType)) {
    return NextResponse.json({ status: "rejected", category: "unsupported_type" }, { status: 415 });
  }
  try {
    const response = await uploadStaffImage(staffId, await file.arrayBuffer(), mediaType);
    if (response.status === 422) {
      const body = (await response.json()) as { detail?: { category?: unknown } };
      const category = typeof body.detail?.category === "string" ? body.detail.category : "invalid_image";
      return NextResponse.json({ status: "rejected", category }, { status: 422 });
    }
    if (!response.ok) return NextResponse.json({ status: "failed" }, { status: response.status });
    return NextResponse.json({ status: "accepted" }, { status: 201 });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
