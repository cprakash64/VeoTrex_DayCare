import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { parseEligibilityUpdate } from "../../../../../../lib/staff-presence";
import { updateEligibility } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ facilityId: string; eligibilityId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { facilityId, eligibilityId } = await params;
  const refused = guardMutation(request, facilityId, eligibilityId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseEligibilityUpdate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateEligibility(facilityId, eligibilityId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
