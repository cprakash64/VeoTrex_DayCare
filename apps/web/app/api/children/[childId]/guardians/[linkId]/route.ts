import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../../lib/classroom-routes";
import { parseLinkUpdate } from "../../../../../../lib/guardians";
import { updateChildLink } from "../../../../../../lib/veotrex-api";

type Context = { params: Promise<{ childId: string; linkId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { childId, linkId } = await params;
  const refused = guardMutation(request, childId, linkId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseLinkUpdate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateChildLink(childId, linkId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
