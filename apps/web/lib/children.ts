/**
 * Child roster and child attendance presentation (V1-04D).
 *
 * Pure functions over the API's own responses, plus the same input rules the API applies so
 * errors appear early. A child here is an operator's roster entry - a display name and an
 * optional identifier - never a photo, a face, a date of birth or a guardian. Names are rendered
 * as text by React, never as markup, and appear only on authenticated operator pages. A camera
 * never checks a child in.
 */

import { UUID_RE } from "./staff-presence";

export type ChildSummary = Readonly<{
  child_id: string;
  facility_id: string;
  display_name: string;
  status: string;
  external_reference: string | null;
  can_administer: boolean;
  created_at: string;
  updated_at: string;
}>;

export type FacilityChildren = Readonly<{
  facility_id: string;
  facility_name: string;
  can_administer: boolean;
  children: ReadonlyArray<ChildSummary>;
}>;

export type ChildCountSummary = Readonly<{
  source: string;
  count: number;
  present: number;
  present_inactive: number;
  stale: number;
  freshness: string;
  valid_until: string | null;
  evaluated_at: string;
}>;

export type AttendanceEntry = Readonly<{
  child_profile_id: string;
  display_name: string;
  status: string;
  counted: boolean;
  state: string;
  location: string;
  other_classroom_id: string | null;
  other_classroom_name: string | null;
  checked_in_at: string | null;
  last_event_at: string | null;
  valid_until: string | null;
}>;

export type AttendanceEvent = Readonly<{
  event_id: string;
  child_profile_id: string;
  display_name: string;
  event_type: string;
  occurred_at: string;
  valid_until: string | null;
  recorded_by_caller: boolean;
  // V1-04E: true only for the check-out of an authorized release.
  released: boolean;
}>;

export type ClassroomAttendance = Readonly<{
  classroom_id: string;
  facility_id: string;
  classroom_active: boolean;
  presence_source_mode: string;
  can_administer: boolean;
  evaluated_at: string;
  lease_min_seconds: number;
  lease_max_seconds: number;
  lease_default_seconds: number;
  summary: ChildCountSummary;
  children: ReadonlyArray<AttendanceEntry>;
  recent_events: ReadonlyArray<AttendanceEvent>;
}>;

// Mirrors the API: 30 minutes to 12 hours, 12 hours by default - a daycare day, never longer
// than the ratio engine trusts any count.
export const ATTENDANCE_LEASE_MIN_SECONDS = 30 * 60;
export const ATTENDANCE_LEASE_MAX_SECONDS = 12 * 60 * 60;
export const DEFAULT_ATTENDANCE_LEASE_SECONDS = 12 * 60 * 60;
export const ATTENDANCE_LEASE_CHOICES: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: 1800, label: "30 minutes" },
  { seconds: 3600, label: "1 hour" },
  { seconds: 4 * 3600, label: "4 hours" },
  { seconds: 8 * 3600, label: "8 hours" },
  { seconds: 10 * 3600, label: "10 hours" },
  { seconds: 12 * 3600, label: "12 hours" },
];

// ------------------------------------------------------------------------------ validation
export const DISPLAY_NAME_MAX = 120;
// Control, invisible formatting (zero-width, bidi overrides), surrogate, private-use,
// unassigned and line/paragraph separator characters: refused, as in the API.
const REFUSED_CHARACTERS = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u;
export const EXTERNAL_REFERENCE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$/;

export function cleanDisplayName(value: string): string | null {
  if (typeof value !== "string") return null;
  const normalised = value.normalize("NFC");
  if (REFUSED_CHARACTERS.test(normalised.trim())) return null;
  const name = normalised.split(/\s+/).filter(Boolean).join(" ");
  if (!name || [...name].length > DISPLAY_NAME_MAX || name.includes("<") || name.includes(">")) return null;
  return name;
}

export function cleanExternalReference(value: string): string | null | undefined {
  const cleaned = value.trim();
  if (!cleaned) return null;
  return EXTERNAL_REFERENCE_PATTERN.test(cleaned) ? cleaned : undefined;
}

export type ChildPayload = Readonly<{ display_name: string; external_reference: string | null }>;

export type ChildValidation =
  | Readonly<{ ok: true; value: ChildPayload }>
  | Readonly<{ ok: false; errors: Readonly<Record<string, string>> }>;

export function validateChildForm(displayName: string, externalReference: string): ChildValidation {
  const errors: Record<string, string> = {};
  const name = cleanDisplayName(displayName);
  if (name === null) {
    errors.display_name = `Enter a name the staff will recognise (up to ${DISPLAY_NAME_MAX} characters, no < or >).`;
  }
  const reference = cleanExternalReference(externalReference);
  if (reference === undefined) {
    errors.external_reference = "Letters, numbers and . _ : / - only (up to 64), or leave empty.";
  }
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value: { display_name: name as string, external_reference: reference ?? null } };
}

function record(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

/** Strict BFF re-validation: a name and an optional identifier - nothing else about a child. */
export function parseChildCreate(body: unknown): ChildPayload | null {
  const value = record(body);
  if (value === null) return null;
  if (Object.keys(value).some((key) => key !== "display_name" && key !== "external_reference")) return null;
  if (typeof value.display_name !== "string") return null;
  if (value.external_reference !== undefined && value.external_reference !== null && typeof value.external_reference !== "string") {
    return null;
  }
  const result = validateChildForm(value.display_name, (value.external_reference as string | null | undefined) ?? "");
  return result.ok ? result.value : null;
}

export type ChildUpdatePayload = Readonly<{ display_name?: string; external_reference?: string | null }>;

export function parseChildUpdate(body: unknown): ChildUpdatePayload | null {
  const value = record(body);
  if (value === null || Object.keys(value).length === 0) return null;
  if (Object.keys(value).some((key) => key !== "display_name" && key !== "external_reference")) return null;
  const payload: { display_name?: string; external_reference?: string | null } = {};
  if ("display_name" in value) {
    if (typeof value.display_name !== "string") return null;
    const name = cleanDisplayName(value.display_name);
    if (name === null) return null;
    payload.display_name = name;
  }
  if ("external_reference" in value) {
    if (value.external_reference === null) payload.external_reference = null;
    else if (typeof value.external_reference === "string") {
      const reference = cleanExternalReference(value.external_reference);
      if (reference === undefined) return null;
      payload.external_reference = reference;
    } else return null;
  }
  return payload;
}

export type AttendancePayload = Readonly<{ child_profile_id: string; lease_seconds?: number }>;

export function validAttendanceLease(seconds: unknown): seconds is number {
  return (
    typeof seconds === "number" &&
    Number.isInteger(seconds) &&
    seconds >= ATTENDANCE_LEASE_MIN_SECONDS &&
    seconds <= ATTENDANCE_LEASE_MAX_SECONDS
  );
}

/** Check-in / refresh: a child UUID and optionally a bounded lease. Never a track or a camera. */
export function parseAttendance(body: unknown, withLease: boolean): AttendancePayload | null {
  const value = record(body);
  if (value === null) return null;
  const allowed = withLease ? ["child_profile_id", "lease_seconds"] : ["child_profile_id"];
  if (Object.keys(value).some((key) => !allowed.includes(key))) return null;
  if (typeof value.child_profile_id !== "string" || !UUID_RE.test(value.child_profile_id)) return null;
  if (!withLease || value.lease_seconds === undefined) return { child_profile_id: value.child_profile_id };
  if (!validAttendanceLease(value.lease_seconds)) return null;
  return { child_profile_id: value.child_profile_id, lease_seconds: value.lease_seconds };
}

// ---------------------------------------------------------------------------- presentation
export function childStatusLabel(status: string): string {
  switch (status) {
    case "ACTIVE":
      return "Active";
    case "INACTIVE":
      return "Inactive";
    case "ARCHIVED":
      return "Archived";
    default:
      return status;
  }
}

function remaining(validUntil: string | null, nowMs: number): number {
  if (!validUntil) return 0;
  const expires = Date.parse(validUntil);
  return Number.isNaN(expires) ? 0 : Math.max(0, Math.floor((expires - nowMs) / 1000));
}

export function formatDuration(seconds: number): string {
  if (seconds >= 3600) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }
  if (seconds >= 60) return `${Math.floor(seconds / 60)} min`;
  return `${seconds} s`;
}

export type AttendanceEntryView = Readonly<{
  state: "here" | "expired" | "elsewhere" | "out";
  label: string;
  tone: "ready" | "inactive" | "attention" | "";
  canCheckIn: boolean;
  canRefresh: boolean;
  canCheckOut: boolean;
}>;

/** One child's line on the classroom card, on the browser's clock. */
export function attendanceEntryView(entry: AttendanceEntry, nowMs: number): AttendanceEntryView {
  const active = entry.status === "ACTIVE";
  if (entry.location === "HERE") {
    const left = remaining(entry.valid_until, nowMs);
    if (entry.state === "PRESENT" && left > 0) {
      return {
        state: "here",
        label: active ? `Present · expires in ${formatDuration(left)}` : `${childStatusLabel(entry.status)} · not counted`,
        tone: active ? "ready" : "attention",
        canCheckIn: false,
        canRefresh: active,
        canCheckOut: true,
      };
    }
    return {
      state: "expired",
      label: "Attendance expired · not counted",
      tone: "attention",
      canCheckIn: active,
      canRefresh: false,
      canCheckOut: true,
    };
  }
  if (entry.location === "OTHER_CLASSROOM") {
    const expired = entry.state !== "PRESENT" || remaining(entry.valid_until, nowMs) <= 0;
    return {
      state: "elsewhere",
      label: `${expired ? "Expired attendance" : "Present"} in ${entry.other_classroom_name ?? "another classroom"}`,
      tone: "inactive",
      canCheckIn: active,
      canRefresh: false,
      canCheckOut: false,
    };
  }
  return { state: "out", label: "Not checked in", tone: "", canCheckIn: active, canRefresh: false, canCheckOut: false };
}

export function attendanceSummaryLines(summary: ChildCountSummary): string[] {
  const lines = [`Children present: ${summary.count} — Attendance`];
  if (summary.present_inactive) lines.push(`Checked in with an inactive or archived profile (not counted): ${summary.present_inactive}`);
  if (summary.stale) lines.push(`Expired attendance (not counted): ${summary.stale}`);
  return lines;
}

const CHILD_MESSAGES: Readonly<Record<string, string>> = {
  invalid_display_name: "Enter a name up to 120 characters, without < or > or invisible characters.",
  invalid_external_reference: "Letters, numbers and . _ : / - only (up to 64).",
  invalid_lease_seconds: "Choose an attendance duration between 30 minutes and 12 hours.",
  external_reference_exists: "Another child at this facility already uses that reference.",
  child_limit_reached: "This facility has reached its child roster limit.",
  child_not_active: "This child is inactive. Reactivate them on the Children page first.",
  child_archived: "This child is archived and cannot be changed or checked in.",
  child_already_checked_in: "Already checked in here. Use Refresh to extend attendance.",
  child_in_another_classroom: "This child is checked into another classroom.",
  child_not_checked_in: "This child is not checked in here.",
  attendance_expired: "Attendance has expired. Check the child in again.",
  classroom_inactive: "Reactivate the classroom first.",
  facility_inactive: "This facility is not active.",
  attendance_state_changed: "Someone else changed this at the same moment. Refresh and try again.",
};

export function childErrorMessage(category: string | null | undefined): string {
  return (category && CHILD_MESSAGES[category]) || "The change could not be saved. Try again.";
}
