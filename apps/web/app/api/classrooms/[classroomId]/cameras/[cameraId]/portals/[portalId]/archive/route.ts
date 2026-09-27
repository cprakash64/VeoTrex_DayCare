import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../../../../../lib/classroom-routes";
import { archiveCameraPortal } from "../../../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; cameraId: string; portalId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, cameraId, portalId } = await params;
  const refused = guardMutation(request, classroomId, cameraId, portalId);
  if (refused) return refused;
  try {
    return await relay(await archiveCameraPortal(classroomId, cameraId, portalId), "archived");
  } catch {
    return refuse(503);
  }
}
