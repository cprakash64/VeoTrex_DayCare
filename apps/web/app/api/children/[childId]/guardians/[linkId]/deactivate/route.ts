import { NextRequest } from "next/server";

import { guardMutation, refuse, relay } from "../../../../../../../lib/classroom-routes";
import { deactivateChildLink } from "../../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ childId: string; linkId: string }> };

export async function POST(request: NextRequest, { params }: Context) {
  const { childId, linkId } = await params;
  const refused = guardMutation(request, childId, linkId);
  if (refused) return refused;
  try {
    return await relay(await deactivateChildLink(childId, linkId), "deactivated");
  } catch {
    return refuse(503);
  }
}
