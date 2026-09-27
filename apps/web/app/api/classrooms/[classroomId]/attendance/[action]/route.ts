import { NextRequest, NextResponse } from "next/server";

import { parseAttendance } from "../../../../../../lib/children";
import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { recordAttendance } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; action: string }> };

const ACTIONS = { "check-in": "checked_in", "check-out": "checked_out", refresh: "refreshed" } as const;

// An operator's explicit child check-in, refresh or check-out (V1-04D). The body names a child
// profile by UUID and, for check-in/refresh, a bounded lease - never a camera or a track.
export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, action } = await params;
  if (!(action in ACTIONS)) return refuse(404);
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseAttendance(body, action !== "check-out");
  if (payload === null) return refuse(422);
  const verb = action as keyof typeof ACTIONS;
  try {
    return await relay(await recordAttendance(classroomId, verb, payload), ACTIONS[verb]);
  } catch {
    return refuse(503);
  }
}
