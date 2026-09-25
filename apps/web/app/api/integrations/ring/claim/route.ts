import { NextRequest, NextResponse } from "next/server";

import { claimRingLink } from "../../../../../lib/veotrex-api";
import { isSameOriginPost, validRingLinkParameters } from "../../../../../lib/ring-link";

type ClaimBody = { nonce?: unknown; time?: unknown };

export async function POST(request: NextRequest) {
  const localRequestId = crypto.randomUUID();
  if (!isSameOriginPost(process.env.APP_BASE_URL, request.headers.get("origin"))) {
    return NextResponse.json({ status: "failed" }, { status: 403 });
  }
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
    return NextResponse.json({ status: "failed" }, { status: 415 });
  }
  let body: ClaimBody;
  try {
    body = (await request.json()) as ClaimBody;
  } catch {
    return NextResponse.json({ status: "failed" }, { status: 400 });
  }
  const time = typeof body.time === "number" ? String(body.time) : null;
  if (
    !validRingLinkParameters(body.nonce, time) ||
    Object.keys(body).some((key) => !["nonce", "time"].includes(key))
  ) {
    return NextResponse.json({ status: "failed" }, { status: 422 });
  }
  try {
    const response = await claimRingLink(body.nonce as string, body.time as number);
    if (!response.ok) {
      return NextResponse.json(
        {
          status: response.status === 409 ? "conflict" : "recoverable_failure",
          support_id: response.headers.get("x-request-id") ?? localRequestId,
        },
        { status: response.status },
      );
    }
    const result = (await response.json()) as { status?: unknown };
    return NextResponse.json({ status: result.status === "ACTIVE" ? "active" : "configuring" });
  } catch {
    return NextResponse.json(
      { status: "recoverable_failure", support_id: localRequestId },
      { status: 503 },
    );
  }
}
