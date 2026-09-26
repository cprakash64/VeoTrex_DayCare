import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { parseRelease } from "../../../../../../lib/guardians";
import { releaseChild } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string }> };

// An operator's authorized release of a child at pickup (V1-04E). The body names exactly a
// child, an adult and the operator's verification method - never a camera, a track, an image,
// an identity-document number or a free-text note.
export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId } = await params;
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseRelease(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await releaseChild(classroomId, payload), "released");
  } catch {
    return refuse(503);
  }
}
