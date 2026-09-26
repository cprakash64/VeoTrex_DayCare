import { NextRequest, NextResponse } from "next/server";

import { parseManualPresence } from "../../../../../lib/classrooms";
import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { submitManualPresence } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId } = await params;
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseManualPresence(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await submitManualPresence(classroomId, payload), "reported");
  } catch {
    return refuse(503);
  }
}
