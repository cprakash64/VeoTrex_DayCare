import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../../lib/classroom-routes";
import { parsePortalCreate } from "../../../../../../../lib/portals";
import { createCameraPortal } from "../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; cameraId: string }> };

// Add a doorway line to one classroom camera (V1-05A): numbers, a side and a label only.
export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, cameraId } = await params;
  const refused = guardMutation(request, classroomId, cameraId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parsePortalCreate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await createCameraPortal(classroomId, cameraId, payload), "created");
  } catch {
    return refuse(503);
  }
}
