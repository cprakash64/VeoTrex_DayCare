import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { parsePresenceMode } from "../../../../../lib/staff-presence";
import { setPresenceSourceMode } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId } = await params;
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parsePresenceMode(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await setPresenceSourceMode(classroomId, payload.mode), "updated");
  } catch {
    return refuse(503);
  }
}
