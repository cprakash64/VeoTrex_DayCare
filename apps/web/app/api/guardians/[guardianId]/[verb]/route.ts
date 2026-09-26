import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../lib/classroom-routes";
import { setGuardianStatus } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ guardianId: string; verb: string }> };

const VERBS = { activate: "activated", deactivate: "deactivated", archive: "archived" } as const;

export async function POST(request: NextRequest, { params }: Context) {
  const { guardianId, verb } = await params;
  if (!(verb in VERBS)) return refuse(404);
  const refused = guardMutation(request, guardianId);
  if (refused) return refused;
  const lifecycle = verb as keyof typeof VERBS;
  try {
    return await relay(await setGuardianStatus(guardianId, lifecycle), VERBS[lifecycle]);
  } catch {
    return refuse(503);
  }
}
