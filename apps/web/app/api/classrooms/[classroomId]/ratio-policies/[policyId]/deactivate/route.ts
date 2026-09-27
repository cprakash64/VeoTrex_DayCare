import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../../../lib/classroom-routes";
import { deactivateRatioPolicy } from "../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string; policyId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { classroomId, policyId } = await params;
  const refused = guardMutation(request, classroomId, policyId);
  if (refused) return refused;
  try {
    return await relay(await deactivateRatioPolicy(classroomId, policyId), "deactivated");
  } catch {
    return refuse(503);
  }
}
