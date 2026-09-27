import { NextRequest, NextResponse } from "next/server";

import { parseChildUpdate } from "../../../../lib/children";
import { guardMutation, readJson, refuse, relay } from "../../../../lib/classroom-routes";
import { updateChild } from "../../../../lib/veotrex-api";

type Context = { params: Promise<{ childId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { childId } = await params;
  const refused = guardMutation(request, childId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseChildUpdate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await updateChild(childId, payload), "updated");
  } catch {
    return refuse(503);
  }
}
