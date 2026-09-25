import { NextRequest, NextResponse } from "next/server";

import { isSameOriginPost } from "../../../../lib/ring-link";
import {
  ACCEPTED_UPLOAD_TYPES,
  MAX_UPLOAD_BYTES,
  faceEvaluationEnabled,
} from "../../../../lib/staff";
import { runRecognitionTest } from "../../../../lib/veotrex-api";

/**
 * Local recognition evaluation (V1-02B0). Multipart from the browser in, raw bounded bytes to
 * the API out - the same shape as an enrollment upload, and the API re-validates the bytes.
 *
 * The route answers 404 unless the evaluation flag is set, so a build that is not an
 * evaluation build has no recognition surface at all, not even one that would forward to an
 * API which would itself refuse. Nothing here writes the photo anywhere: the bytes are read
 * once from the request and handed straight to the API call.
 */
export async function POST(request: NextRequest) {
  if (!faceEvaluationEnabled(process.env)) {
    return NextResponse.json({ status: "unavailable" }, { status: 404 });
  }
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
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
    const response = await runRecognitionTest(await file.arrayBuffer(), mediaType);
    if (response.status === 422) {
      const body = (await response.json()) as { detail?: { category?: unknown } };
      const category = typeof body.detail?.category === "string" ? body.detail.category : "invalid_image";
      return NextResponse.json({ status: "rejected", category }, { status: 422 });
    }
    if (!response.ok) return NextResponse.json({ status: "failed" }, { status: response.status });
    // The API's evaluation result is already free of embeddings and of any identity it
    // refused to name, so it is forwarded as-is rather than reshaped here.
    return NextResponse.json({ status: "completed", result: await response.json() }, { status: 200 });
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 503 });
  }
}
