import { NextRequest, NextResponse } from "next/server";

import { parsePolicyPayload } from "../../../../../../lib/classrooms";
import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { updateRatioPolicy } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; policyId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { classroomId, policyId } = await params;
  const refused = guardMutation(request, classroomId, policyId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parsePolicyPayload(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateRatioPolicy(classroomId, policyId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
