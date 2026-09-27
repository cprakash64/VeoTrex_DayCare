import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { parseEligibilityCreate } from "../../../../../lib/staff-presence";
import { createEligibility } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ facilityId: string }> };

// Add a staff profile to a facility's roster, counting toward the configured classroom policy
// or not (V1-04C). An operator designation, never a qualification check.
export async function POST(request: NextRequest, { params }: Context) {
  const { facilityId } = await params;
  const refused = guardMutation(request, facilityId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseEligibilityCreate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await createEligibility(facilityId, payload), "created");
  } catch {
    return refuse(503);
  }
}
