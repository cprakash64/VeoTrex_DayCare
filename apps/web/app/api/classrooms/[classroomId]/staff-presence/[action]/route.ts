import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { parseStaffPresence } from "../../../../../../lib/staff-presence";
import { recordStaffPresence } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; action: string }> };

const ACTIONS = { "check-in": "checked_in", "check-out": "checked_out", refresh: "refreshed" } as const;

// An operator's explicit staff check-in, refresh or check-out (V1-04C). The body names a staff
// profile and, for check-in/refresh, a bounded lease - nothing else: no time, image or track.
export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, action } = await params;
  if (!(action in ACTIONS)) return refuse(404);
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseStaffPresence(body, action !== "check-out");
  if (payload === null) return refuse(422);
  const verb = action as keyof typeof ACTIONS;
  try {
    return await relay(await recordStaffPresence(classroomId, verb, payload), ACTIONS[verb]);
  } catch {
    return refuse(503);
  }
}
