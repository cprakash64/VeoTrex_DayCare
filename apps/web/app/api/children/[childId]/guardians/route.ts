import { NextRequest, NextResponse } from "next/server";

import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { parseLinkCreate } from "../../../../../lib/guardians";
import { createChildLink } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ childId: string }> };

// Associate an adult contact with a child (V1-04E). The relationship label is the operator's
// own words; pickup authorization is the separate, explicit flag.
export async function POST(request: NextRequest, { params }: Context) {
  const { childId } = await params;
  const refused = guardMutation(request, childId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseLinkCreate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await createChildLink(childId, payload), "created");
  } catch {
    return refuse(503);
  }
}
