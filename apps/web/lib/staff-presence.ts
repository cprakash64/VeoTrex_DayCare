/**
 * Staff roster, ratio eligibility and staff check-in presentation (V1-04C).
 *
 * Pure functions over the API's own responses. The backend decides who counts; this module only
 * words it and validates operator input before it is sent. Rules enforced here as in the API:
 *
 * - "Counts toward the configured classroom policy" is an operator's designation. Nothing here
 *   claims a licence, a certification or any legal qualification for a person.
 * - Presence is an operator check-in with a bounded lease. A camera, a face match or an unknown
 *   person never checks anyone in.
 * - Staff names shown here are enrolled adult staff profiles. There is no child identity.
 */

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const ROSTER_MODE = "ROSTER_STAFF_PLUS_MANUAL_CHILDREN";
export const MANUAL_MODE = "MANUAL_AGGREGATE";
export const PRESENCE_MODES = [MANUAL_MODE, ROSTER_MODE] as const;
export type PresenceMode = (typeof PRESENCE_MODES)[number];

// Mirrors the API: 1 minute to 4 hours, 15 minutes by default. Never unbounded.
export const LEASE_MIN_SECONDS = 60;
export const LEASE_MAX_SECONDS = 4 * 60 * 60;
export const DEFAULT_LEASE_SECONDS = 15 * 60;
export const LEASE_CHOICES: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: 300, label: "5 minutes" },
  { seconds: 900, label: "15 minutes" },
  { seconds: 1800, label: "30 minutes" },
  { seconds: 3600, label: "1 hour" },
  { seconds: 7200, label: "2 hours" },
  { seconds: 14400, label: "4 hours" },
];

export type StaffCountSummary = Readonly<{
  source: string;
  count: number;
  present: number;
  present_ratio_ineligible: number;
  present_inactive: number;
  present_ambiguous: number;
  stale: number;
  freshness: string;
  valid_until: string | null;
  evaluated_at: string;
}>;

export type StaffPresenceEntry = Readonly<{
  staff_profile_id: string;
  display_name: string;
  staff_status: string;
  on_facility_roster: boolean;
  counts_toward_ratio: boolean;
  counted: boolean;
  state: string;
  location: string;
  other_classroom_id: string | null;
  other_classroom_name: string | null;
  checked_in_at: string | null;
  last_event_at: string | null;
  valid_until: string | null;
}>;

export type StaffPresenceEvent = Readonly<{
  event_id: string;
  staff_profile_id: string;
  display_name: string;
  event_type: string;
  occurred_at: string;
  valid_until: string | null;
  recorded_by_caller: boolean;
}>;

export type ClassroomStaffPresence = Readonly<{
  classroom_id: string;
  facility_id: string;
  classroom_active: boolean;
  presence_source_mode: string;
  can_administer: boolean;
  evaluated_at: string;
  lease_min_seconds: number;
  lease_max_seconds: number;
  lease_default_seconds: number;
  summary: StaffCountSummary;
  staff: ReadonlyArray<StaffPresenceEntry>;
  recent_events: ReadonlyArray<StaffPresenceEvent>;
}>;

export type EligibilityAssignment = Readonly<{
  eligibility_id: string;
  facility_id: string;
  staff_profile_id: string;
  staff_display_name: string;
  staff_status: string;
  status: string;
  counts_toward_ratio: boolean;
  note: string | null;
  effective_from: string;
  effective_until: string | null;
  effective_from_date: string;
  effective_through_date: string | null;
  in_effect: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
  deactivated_at: string | null;
  eligibility_basis: string;
}>;

export type FacilityRoster = Readonly<{
  facility_id: string;
  facility_name: string;
  facility_timezone: string;
  can_administer: boolean;
  assignments: ReadonlyArray<EligibilityAssignment>;
}>;

// ------------------------------------------------------------------------- BFF parsers
export type StaffPresencePayload = Readonly<{ staff_profile_id: string; lease_seconds?: number }>;
export type EligibilityCreatePayload = Readonly<{
  staff_profile_id: string;
  counts_toward_ratio: boolean;
  note: string | null;
}>;
export type EligibilityUpdatePayload = Readonly<{ counts_toward_ratio: boolean }>;

const NOTE_TEXT = /^[^\u0000-\u001f\u007f<>]+$/;

function record(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

function onlyKeys(value: Record<string, unknown>, allowed: ReadonlyArray<string>): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

export function validLease(seconds: unknown): seconds is number {
  return (
    typeof seconds === "number" &&
    Number.isInteger(seconds) &&
    seconds >= LEASE_MIN_SECONDS &&
    seconds <= LEASE_MAX_SECONDS
  );
}

/** Check-in and refresh: exactly a staff profile id and, optionally, a bounded lease. */
export function parseStaffPresence(body: unknown, withLease: boolean): StaffPresencePayload | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, withLease ? ["staff_profile_id", "lease_seconds"] : ["staff_profile_id"])) {
    return null;
  }
  if (typeof value.staff_profile_id !== "string" || !UUID_RE.test(value.staff_profile_id)) return null;
  if (!withLease || value.lease_seconds === undefined) return { staff_profile_id: value.staff_profile_id };
  if (!validLease(value.lease_seconds)) return null;
  return { staff_profile_id: value.staff_profile_id, lease_seconds: value.lease_seconds };
}

export function parseEligibilityCreate(body: unknown): EligibilityCreatePayload | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, ["staff_profile_id", "counts_toward_ratio", "note"])) return null;
  if (typeof value.staff_profile_id !== "string" || !UUID_RE.test(value.staff_profile_id)) return null;
  if (typeof value.counts_toward_ratio !== "boolean") return null;
  let note: string | null = null;
  if (value.note !== undefined && value.note !== null) {
    if (typeof value.note !== "string") return null;
    const cleaned = value.note.split(/\s+/).filter(Boolean).join(" ");
    if (cleaned && (cleaned.length > 500 || !NOTE_TEXT.test(cleaned))) return null;
    note = cleaned || null;
  }
  return { staff_profile_id: value.staff_profile_id, counts_toward_ratio: value.counts_toward_ratio, note };
}

export function parseEligibilityUpdate(body: unknown): EligibilityUpdatePayload | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, ["counts_toward_ratio"])) return null;
  if (typeof value.counts_toward_ratio !== "boolean") return null;
  return { counts_toward_ratio: value.counts_toward_ratio };
}

export function parsePresenceMode(body: unknown): { mode: PresenceMode } | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, ["mode"])) return null;
  return PRESENCE_MODES.includes(value.mode as PresenceMode) ? { mode: value.mode as PresenceMode } : null;
}

// ------------------------------------------------------------------------ presentation
export const COUNTS_LABEL = "Counts toward configured classroom policy";
export const NOT_COUNTED_LABEL = "Does not count toward configured classroom policy";

export function eligibilityLabel(counts: boolean): string {
  return counts ? COUNTS_LABEL : NOT_COUNTED_LABEL;
}

export function sourceLabel(source: string | null | undefined): string {
  switch (source) {
    case "MANUAL":
      return "Manual";
    case "STAFF_ROSTER":
      return "Staff roster";
    default:
      return "Not connected";
  }
}

export function presenceModeLabel(mode: string): string {
  return mode === ROSTER_MODE
    ? "Staff from check-ins, children from the manual report"
    : "All counts from the manual report";
}

export function remainingSeconds(validUntil: string | null, nowMs: number): number | null {
  if (!validUntil) return null;
  const expires = Date.parse(validUntil);
  if (Number.isNaN(expires)) return null;
  return Math.max(0, Math.floor((expires - nowMs) / 1000));
}

export function formatRemaining(seconds: number): string {
  if (seconds >= 3600) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }
  if (seconds >= 60) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  return `${seconds} s`;
}

export type StaffEntryView = Readonly<{
  state: "here" | "expired" | "elsewhere" | "other_facility" | "out";
  label: string;
  tone: "ready" | "inactive" | "attention" | "";
  canCheckIn: boolean;
  canRefresh: boolean;
  canCheckOut: boolean;
}>;

/**
 * One person's line on the classroom card, on the browser's clock: a check-in that has run out
 * since the page loaded reads as expired immediately, never as present.
 */
export function staffEntryView(entry: StaffPresenceEntry, nowMs: number): StaffEntryView {
  const active = entry.staff_status === "ACTIVE";
  const assigned = entry.on_facility_roster;
  if (entry.location === "HERE") {
    const remaining = remainingSeconds(entry.valid_until, nowMs);
    if (entry.state === "PRESENT" && remaining !== null && remaining > 0) {
      return {
        state: "here",
        label: `Checked in · expires in ${formatRemaining(remaining)}`,
        tone: "ready",
        canCheckIn: false,
        canRefresh: active && assigned,
        canCheckOut: true,
      };
    }
    return {
      state: "expired",
      label: "Check-in expired · not counted",
      tone: "attention",
      canCheckIn: active && assigned,
      canRefresh: false,
      canCheckOut: true,
    };
  }
  if (entry.location === "OTHER_CLASSROOM") {
    const expired = entry.state !== "PRESENT" || (remainingSeconds(entry.valid_until, nowMs) ?? 0) <= 0;
    return {
      state: "elsewhere",
      label: `${expired ? "Expired check-in" : "Checked in"} in ${entry.other_classroom_name ?? "another classroom"}`,
      tone: "inactive",
      canCheckIn: active && assigned,
      canRefresh: false,
      canCheckOut: false,
    };
  }
  if (entry.location === "OTHER_FACILITY") {
    return {
      state: "other_facility",
      label: entry.state === "PRESENT" ? "Checked in at another facility" : "Not checked in",
      tone: "inactive",
      canCheckIn: active && assigned && entry.state !== "PRESENT",
      canRefresh: false,
      canCheckOut: false,
    };
  }
  return {
    state: "out",
    label: "Not checked in",
    tone: "",
    canCheckIn: active && assigned,
    canRefresh: false,
    canCheckOut: false,
  };
}

/** Why a checked-in person is or is not in the count. Never a legal statement. */
export function countedLabel(entry: StaffPresenceEntry): string {
  if (entry.staff_status !== "ACTIVE") return "Inactive profile · not counted";
  if (!entry.on_facility_roster) return "Not on this facility's roster · not counted";
  return entry.counts_toward_ratio ? COUNTS_LABEL : NOT_COUNTED_LABEL;
}

export function rosterSummaryLines(summary: StaffCountSummary): string[] {
  const lines = [`Qualified staff present: ${summary.count} — Staff roster`];
  if (summary.present_ratio_ineligible) {
    lines.push(`Checked in but not counted toward the configured ratio: ${summary.present_ratio_ineligible}`);
  }
  if (summary.present_inactive) lines.push(`Checked in with an inactive profile (not counted): ${summary.present_inactive}`);
  if (summary.present_ambiguous) lines.push(`Conflicting designations (not counted): ${summary.present_ambiguous}`);
  if (summary.stale) lines.push(`Expired check-ins (not counted): ${summary.stale}`);
  return lines;
}

const STAFF_MESSAGES: Readonly<Record<string, string>> = {
  staff_not_active: "This staff profile is inactive. Reactivate it on the Staff page first.",
  staff_not_assigned_to_facility: "Add this person to the facility roster on their Staff page first.",
  staff_already_checked_in: "Already checked in here. Use Refresh to extend the check-in.",
  staff_checked_in_elsewhere: "Checked in at another facility. Check them out there first.",
  staff_in_another_classroom: "This person is checked into another classroom.",
  staff_not_checked_in: "This person is not checked in here.",
  staff_presence_expired: "The check-in has expired. Check them in again.",
  classroom_inactive: "Reactivate the classroom first.",
  facility_inactive: "This facility is not active.",
  eligibility_exists: "This person is already on this facility's roster.",
  eligibility_inactive: "That roster entry was removed; add a new one.",
  invalid_lease_seconds: "Choose a check-in duration between 1 minute and 4 hours.",
  invalid_eligibility_note: "Up to 500 characters, no < or >.",
  presence_state_changed: "Someone else changed this at the same moment. Refresh and try again.",
  staff_count_comes_from_roster: "Staff are counted from check-ins in this classroom; report children and visitors only.",
};

export function staffErrorMessage(category: string | null | undefined): string {
  return (category && STAFF_MESSAGES[category]) || "The change could not be saved. Try again.";
}
