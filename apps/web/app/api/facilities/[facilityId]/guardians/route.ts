import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { parseGuardianCreate } from "../../../../../lib/guardians";
import { createGuardian } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ facilityId: string }> };

// Add an adult contact to a facility (V1-04E): a display name and an optional identifier only.
export async function POST(request: NextRequest, { params }: Context) {
  const { facilityId } = await params;
  const refused = guardMutation(request, facilityId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseGuardianCreate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await createGuardian(facilityId, payload), "created");
  } catch {
    return refuse(503);
  }
}
