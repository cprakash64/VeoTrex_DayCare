import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../../../lib/classroom-routes";
import { revokeManualPresence } from "../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; snapshotId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, snapshotId } = await params;
  const refused = guardMutation(request, classroomId, snapshotId);
  if (refused) return refused;
  try {
    return await relay(await revokeManualPresence(classroomId, snapshotId), "revoked");
  } catch {
    return refuse(503);
  }
}
