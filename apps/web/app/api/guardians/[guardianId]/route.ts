import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../lib/classroom-routes";
import { parseGuardianUpdate } from "../../../../lib/guardians";
import { updateGuardian } from "../../../../lib/veotrex-api";

type Context = { params: Promise<{ guardianId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { guardianId } = await params;
  const refused = guardMutation(request, guardianId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseGuardianUpdate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateGuardian(guardianId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
