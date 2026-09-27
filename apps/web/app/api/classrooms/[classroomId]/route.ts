import { NextRequest, NextResponse } from "next/server";

import { validateClassroomForm } from "../../../../lib/classrooms";
import { guardMutation, readJson, refuse, relay } from "../../../../lib/classroom-routes";
import { updateClassroom } from "../../../../lib/veotrex-api";

type Context = { params: Promise<{ classroomId: string }> };

export async function PATCH(request: NextRequest, { params }: Context) {
  const { classroomId } = await params;
  const refused = guardMutation(request, classroomId);
  if (refused) return refused;
  const body = await readJson(request);
  if (body instanceof NextResponse) return body;
  const record = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (Object.keys(record).some((key) => !["name", "age_band_label"].includes(key))) return refuse(422);
  const checked = validateClassroomForm(
    typeof record.name === "string" ? record.name : "",
    typeof record.age_band_label === "string" ? record.age_band_label : "",
  );
  if (!checked.ok) return refuse(422);
  try {
    return await relay(await updateClassroom(classroomId, checked.value), "updated");
  } catch {
    return refuse(503);
  }
}
