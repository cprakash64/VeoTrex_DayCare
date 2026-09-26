import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { setClassroomActive } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId } = await params;
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const active = (body as { active?: unknown } | null)?.active;
  if (typeof active !== "boolean") return refuse(422);
  try {
    return await relay(await setClassroomActive(classroomId, active), active ? "activated" : "deactivated");
  } catch {
    return refuse(503);
  }
}
