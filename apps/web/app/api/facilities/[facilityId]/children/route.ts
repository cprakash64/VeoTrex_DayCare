import { NextRequest, NextResponse } from "next/server";

import { parseChildCreate } from "../../../../../lib/children";
import { guardMutation, readJson, refuse, relay } from "../../../../../lib/classroom-routes";
import { createChild } from "../../../../../lib/veotrex-api";

type Context = { params: Promise<{ facilityId: string }> };

// Add a child to a facility roster (V1-04D): a display name and an optional identifier only.
export async function POST(request: NextRequest, { params }: Context) {
  const { facilityId } = await params;
  const refused = guardMutation(request, facilityId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const payload = parseChildCreate(body);
  if (payload === null) return refuse(422);
  try {
    return await relay(await createChild(facilityId, payload), "created");
  } catch {
    return refuse(503);
  }
}
