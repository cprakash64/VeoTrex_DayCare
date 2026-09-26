import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../../../lib/classroom-routes";
import { deactivateEligibility } from "../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ facilityId: string; eligibilityId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { facilityId, eligibilityId } = await params;
  const refused = guardMutation(request, facilityId, eligibilityId);
  if (refused) return refused;
  try {
    return await relay(await deactivateEligibility(facilityId, eligibilityId), "deactivated");
  } catch {
    return refuse(503);
  }
}
