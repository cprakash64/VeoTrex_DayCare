import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../lib/classroom-routes";
import { setChildStatus } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ childId: string; verb: string }> };

const VERBS = { activate: "activated", deactivate: "deactivated", archive: "archived" } as const;

export async function POST(request: NextRequest, { params }: Context) {
  const { childId, verb } = await params;
  if (!(verb in VERBS)) return refuse(404);
  const refused = guardMutation(request, childId);
  if (refused) return refused;
  const lifecycle = verb as keyof typeof VERBS;
  try {
    return await relay(await setChildStatus(childId, lifecycle), VERBS[lifecycle]);
  } catch {
    return refuse(503);
  }
}
