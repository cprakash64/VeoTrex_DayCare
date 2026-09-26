import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../../../lib/classroom-routes";
import { parsePortalUpdate } from "../../../../../../../../lib/portals";
import { updateCameraPortal } from "../../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; cameraId: string; portalId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { classroomId, cameraId, portalId } = await params;
  const refused = guardMutation(request, classroomId, cameraId, portalId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parsePortalUpdate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateCameraPortal(classroomId, cameraId, portalId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
