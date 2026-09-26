import { NextRequest, NextResponse } from "next/server";

import { UUID_PATTERN } from "../../../lib/ring-inventory";
import { validateClassroomForm } from "../../../lib/classrooms";
import { guardMutation, readJson, refuse, relay } from "../../../lib/classroom-routes";
import { createClassroom } from "../../../lib/veotrex-api";

export async function POST(request: NextRequest) {
  const refused = guardMutation(request);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const record = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (Object.keys(record).some((key) => !["facility_id", "name", "age_band_label"].includes(key))) {
    return refuse(422);
  }
  const facilityId = typeof record.facility_id === "string" ? record.facility_id : "";
  if (!UUID_PATTERN.test(facilityId)) return refuse(422);
  const checked = validateClassroomForm(
    typeof record.name === "string" ? record.name : "",
    typeof record.age_band_label === "string" ? record.age_band_label : "",
  );
  if (!checked.ok) return refuse(422);
  try {
    return await relay(await createClassroom(facilityId, checked.value), "created");
  } catch {
    return refuse(503);
  }
}
