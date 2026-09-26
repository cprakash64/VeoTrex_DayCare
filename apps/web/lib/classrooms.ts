/**
 * Classroom and configured ratio policy presentation (V1-04A).
 *
 * Pure functions over the API's own responses: the backend decides the ratio state, this module
 * only words it and validates operator input before it is sent. Two rules are enforced here as
 * well as in the API:
 *
 * - A policy is a *configured classroom policy*. Nothing here says compliant, legal or names a
 *   jurisdiction, because nothing verified one.
 * - A camera never counts children. People the camera sees are "people seen by the camera",
 *   and people nobody accounts for are "unexplained", never children or staff.
 */

export type FacilitySummary = Readonly<{
  facility_id: string;
  name: string;
  timezone: string;
  status: string;
  can_administer: boolean;
}>;

export type ClassroomCamera = Readonly<{
  camera_id: string;
  name: string;
  status: string;
  zone_name: string;
}>;

export type RatioPolicy = Readonly<{
  policy_id: string;
  label: string;
  age_band_label: string | null;
  max_children_per_staff: number;
  minimum_staff: number;
  maximum_group_size: number | null;
  effective_from: string;
  effective_until: string | null;
  effective_from_date: string;
  effective_through_date: string | null;
  status: string;
  revision: number;
  source_reference: string | null;
  in_effect: boolean;
  created_at: string;
  updated_at: string;
}>;

export type Classroom = Readonly<{
  classroom_id: string;
  facility_id: string;
  facility_name: string;
  facility_timezone: string;
  name: string;
  status: string;
  age_band_label: string | null;
  cameras: ReadonlyArray<ClassroomCamera>;
  policies: ReadonlyArray<RatioPolicy>;
  current_policy_id: string | null;
  can_administer: boolean;
  policy_basis: string;
  created_at: string;
  updated_at: string;
}>;

export type RatioEvaluation = Readonly<{
  ratio_state: string;
  conditions: ReadonlyArray<string>;
  explanations: ReadonlyArray<string>;
  child_count: number | null;
  staff_count: number | null;
  max_children_per_staff: number | null;
  minimum_staff: number | null;
  required_staff: number | null;
  staff_deficit: number | null;
  group_size: number | null;
  maximum_group_size: number | null;
  child_freshness: string;
  staff_freshness: string;
  evaluated_at: string;
}>;

export type VisionReconciliation = Readonly<{
  state: string;
  authoritative_expected_people: number | null;
  vision_observed_people: number | null;
  unexplained_observed_people: number | null;
  unseen_expected_people: number | null;
  reasons: ReadonlyArray<string>;
}>;

export type PresenceStatus = Readonly<{
  availability: string;
  source: string | null;
  snapshot_id: string | null;
  observed_at: string | null;
  valid_until: string | null;
  freshness: string;
  child_count: number | null;
  qualified_staff_count: number | null;
  visitor_count: number | null;
}>;

export type RatioStatus = Readonly<{
  classroom_id: string;
  evaluation: RatioEvaluation;
  reconciliation: VisionReconciliation;
  presence_connected: boolean;
  vision_connected: boolean;
  policy_basis: string;
  presence?: PresenceStatus;
}>;

export type PresenceReport = Readonly<{
  snapshot_id: string;
  source: string;
  child_count: number;
  qualified_staff_count: number;
  visitor_count: number;
  observed_at: string;
  valid_until: string;
  created_at: string;
  revoked_at: string | null;
  freshness: string;
  authoritative: boolean;
  submitted_by_caller: boolean;
}>;

export type ClassroomPresence = Readonly<{
  classroom_id: string;
  availability: string;
  current: PresenceReport | null;
  history: ReadonlyArray<PresenceReport>;
}>;

// ------------------------------------------------------------- manual presence (V1-04B)
// Mirrors the API's bounds. Counts only: there is no field for a name or an identifier, and
// the report's time is the server's clock at submission.
export const MANUAL_LIMITS = { children: 150, qualified_staff: 50, visitors: 50 } as const;
export const VALIDITY_CHOICES: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: 30, label: "30 seconds" },
  { seconds: 60, label: "1 minute" },
  { seconds: 120, label: "2 minutes" },
  { seconds: 300, label: "5 minutes" },
  { seconds: 600, label: "10 minutes" },
  { seconds: 900, label: "15 minutes" },
];
export const DEFAULT_VALIDITY_SECONDS = 120;

export type ManualPresenceForm = Readonly<{
  child_count: string;
  qualified_staff_count: string;
  visitor_count: string;
  valid_for_seconds: string;
}>;

export type ManualPresencePayload = Readonly<{
  child_count: number;
  qualified_staff_count: number;
  visitor_count: number;
  valid_for_seconds: number;
}>;

// --------------------------------------------------------------------------- validation
export const AGE_BAND_PATTERN = /^[A-Za-z0-9 .,'()/+&_-]+$/;
const FREE_TEXT = /^[^\u0000-\u001f\u007f<>]+$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export type PolicyFormInput = Readonly<{
  label: string;
  age_band_label: string;
  max_children_per_staff: string;
  minimum_staff: string;
  maximum_group_size: string;
  effective_from_date: string;
  effective_through_date: string;
  source_reference: string;
}>;

export type PolicyPayload = Readonly<{
  label: string;
  age_band_label: string | null;
  max_children_per_staff: number;
  minimum_staff: number;
  maximum_group_size: number | null;
  effective_from_date: string;
  effective_through_date: string | null;
  source_reference: string | null;
}>;

export type Validation<T> =
  | Readonly<{ ok: true; value: T }>
  | Readonly<{ ok: false; errors: Readonly<Record<string, string>> }>;

function integer(text: string, minimum: number, maximum: number): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum ? value : null;
}

function isRealDate(text: string): boolean {
  if (!ISO_DATE.test(text)) return false;
  const parsed = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === text;
}

function optionalText(value: string, maximum: number, pattern: RegExp): string | null | undefined {
  const cleaned = value.split(/\s+/).filter(Boolean).join(" ");
  if (!cleaned) return null;
  return cleaned.length <= maximum && pattern.test(cleaned) ? cleaned : undefined;
}

/** Operator input -> API payload, with every rule the API applies, so errors appear early. */
export function validatePolicyForm(input: PolicyFormInput): Validation<PolicyPayload> {
  const errors: Record<string, string> = {};
  const label = input.label.split(/\s+/).filter(Boolean).join(" ");
  if (!label || label.length > 120 || !FREE_TEXT.test(label)) {
    errors.label = "Enter a policy label (up to 120 characters, no < or >).";
  }
  const ageBand = optionalText(input.age_band_label, 64, AGE_BAND_PATTERN);
  if (ageBand === undefined) errors.age_band_label = "Use letters, numbers and simple punctuation (up to 64).";
  const ratio = integer(input.max_children_per_staff, 1, 100);
  if (ratio === null) errors.max_children_per_staff = "Enter a whole number from 1 to 100.";
  const minimum = integer(input.minimum_staff, 0, 50);
  if (minimum === null) errors.minimum_staff = "Enter a whole number from 0 to 50.";
  let group: number | null = null;
  if (input.maximum_group_size.trim()) {
    group = integer(input.maximum_group_size, 1, 500);
    if (group === null) errors.maximum_group_size = "Leave empty, or enter a whole number from 1 to 500.";
  }
  if (!isRealDate(input.effective_from_date)) errors.effective_from_date = "Choose the first day the policy applies.";
  const through = input.effective_through_date.trim();
  if (through && !isRealDate(through)) errors.effective_through_date = "Choose a valid last day, or leave empty.";
  else if (through && isRealDate(input.effective_from_date) && through < input.effective_from_date) {
    errors.effective_through_date = "The last day cannot be before the first day.";
  }
  const source = optionalText(input.source_reference, 500, FREE_TEXT);
  if (source === undefined) errors.source_reference = "Up to 500 characters, no < or >.";
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return {
    ok: true,
    value: {
      label,
      age_band_label: ageBand ?? null,
      max_children_per_staff: ratio as number,
      minimum_staff: minimum as number,
      maximum_group_size: group,
      effective_from_date: input.effective_from_date,
      effective_through_date: through || null,
      source_reference: source ?? null,
    },
  };
}

/** Strict re-validation of a JSON body in the BFF: exact keys, exact types. */
export function parsePolicyPayload(body: unknown): PolicyPayload | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const record = body as Record<string, unknown>;
  const allowed = new Set([
    "label",
    "age_band_label",
    "max_children_per_staff",
    "minimum_staff",
    "maximum_group_size",
    "effective_from_date",
    "effective_through_date",
    "source_reference",
  ]);
  if (Object.keys(record).some((key) => !allowed.has(key))) return null;
  const asText = (value: unknown) => (typeof value === "string" ? value : value == null ? "" : null);
  const asNumber = (value: unknown) =>
    typeof value === "number" && Number.isInteger(value) ? String(value) : value == null ? "" : null;
  const fields = {
    label: asText(record.label),
    age_band_label: asText(record.age_band_label),
    max_children_per_staff: asNumber(record.max_children_per_staff),
    minimum_staff: asNumber(record.minimum_staff),
    maximum_group_size: asNumber(record.maximum_group_size),
    effective_from_date: asText(record.effective_from_date),
    effective_through_date: asText(record.effective_through_date),
    source_reference: asText(record.source_reference),
  };
  if (Object.values(fields).some((value) => value === null)) return null;
  const result = validatePolicyForm(fields as PolicyFormInput);
  return result.ok ? result.value : null;
}

export function validateManualPresence(input: ManualPresenceForm): Validation<ManualPresencePayload> {
  const errors: Record<string, string> = {};
  const children = integer(input.child_count, 0, MANUAL_LIMITS.children);
  if (children === null) errors.child_count = `Enter a whole number from 0 to ${MANUAL_LIMITS.children}.`;
  const staff = integer(input.qualified_staff_count, 0, MANUAL_LIMITS.qualified_staff);
  if (staff === null) errors.qualified_staff_count = `Enter a whole number from 0 to ${MANUAL_LIMITS.qualified_staff}.`;
  const visitors = input.visitor_count.trim() ? integer(input.visitor_count, 0, MANUAL_LIMITS.visitors) : 0;
  if (visitors === null) errors.visitor_count = `Enter a whole number from 0 to ${MANUAL_LIMITS.visitors}.`;
  const validity = Number(input.valid_for_seconds);
  if (!VALIDITY_CHOICES.some((choice) => choice.seconds === validity)) {
    errors.valid_for_seconds = "Choose how long this count stays valid.";
  }
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return {
    ok: true,
    value: {
      child_count: children as number,
      qualified_staff_count: staff as number,
      visitor_count: visitors as number,
      valid_for_seconds: validity,
    },
  };
}

/** Strict re-validation in the BFF: exactly these keys, integer values only. */
export function parseManualPresence(body: unknown): ManualPresencePayload | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const record = body as Record<string, unknown>;
  const keys = ["child_count", "qualified_staff_count", "visitor_count", "valid_for_seconds"];
  if (Object.keys(record).some((key) => !keys.includes(key))) return null;
  if (keys.some((key) => typeof record[key] !== "number" || !Number.isInteger(record[key]))) return null;
  const result = validateManualPresence({
    child_count: String(record.child_count),
    qualified_staff_count: String(record.qualified_staff_count),
    visitor_count: String(record.visitor_count),
    valid_for_seconds: String(record.valid_for_seconds),
  });
  return result.ok ? result.value : null;
}

export function presenceStateLabel(report: PresenceReport, nowMs: number): string {
  if (report.revoked_at) return "Revoked";
  if (report.freshness === "FRESH" && Date.parse(report.valid_until) > nowMs) return "Fresh";
  return "Stale";
}

export type ClassroomPayload = Readonly<{ name: string; age_band_label: string | null }>;

export function validateClassroomForm(
  name: string,
  ageBand: string,
): Validation<ClassroomPayload> {
  const errors: Record<string, string> = {};
  const cleaned = name.split(/\s+/).filter(Boolean).join(" ");
  if (!cleaned || cleaned.length > 200 || !FREE_TEXT.test(cleaned)) {
    errors.name = "Enter a classroom name (up to 200 characters, no < or >).";
  }
  const band = optionalText(ageBand, 64, AGE_BAND_PATTERN);
  if (band === undefined) errors.age_band_label = "Use letters, numbers and simple punctuation (up to 64).";
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value: { name: cleaned, age_band_label: band ?? null } };
}

const API_MESSAGES: Readonly<Record<string, string>> = {
  classroom_name_exists: "Another room in this facility already has that name.",
  classroom_limit_reached: "This facility has reached its classroom limit.",
  classroom_inactive: "Reactivate the classroom before changing its policy.",
  facility_inactive: "This facility is not active.",
  facility_timezone_invalid: "This facility's timezone is not configured correctly.",
  policy_period_overlaps: "Another active policy for this classroom covers some of these dates.",
  policy_limit_reached: "This classroom has reached its policy limit.",
  policy_inactive: "A deactivated policy cannot be edited; create a new one.",
  invalid_child_count: "Children present must be a whole number within the allowed range.",
  invalid_qualified_staff_count: "Qualified staff present must be a whole number within the allowed range.",
  invalid_visitor_count: "Visitors present must be a whole number within the allowed range.",
  invalid_validity_seconds: "Choose how long this count stays valid.",
  effective_period_inverted: "The last day cannot be before the first day.",
};

export function apiErrorMessage(category: string | null | undefined): string {
  return (category && API_MESSAGES[category]) || "The change could not be saved. Check the values and try again.";
}

// ------------------------------------------------------------------------- presentation
export type Tone = "neutral" | "ok" | "attention";

export type RatioStatusView = Readonly<{
  headline: string;
  tone: Tone;
  details: ReadonlyArray<string>;
  basis: string;
}>;

const BASIS = "Configured classroom policy";

function clock(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleTimeString();
}

/**
 * The server's evaluation, worded. ``nowMs`` lets the page re-check freshness on the browser's
 * clock: a result computed while a report was fresh is never shown after it has expired.
 */
export function ratioStatusView(status: RatioStatus | null, nowMs: number = Date.now()): RatioStatusView {
  if (status === null) {
    return { headline: "Ratio data unavailable", tone: "neutral", details: [], basis: BASIS };
  }
  const { evaluation, presence } = status;
  const details: string[] = [];
  const explanations = new Set(evaluation.explanations);
  const expiredHere =
    presence?.availability === "PRESENCE_FRESH" &&
    presence.valid_until !== null &&
    Date.parse(presence.valid_until) <= nowMs;
  if (expiredHere && evaluation.ratio_state !== "NOT_CONFIGURED") {
    return {
      headline: "Presence data stale",
      tone: "neutral",
      details: ["The operator-reported count has expired. Report current presence to update."],
      basis: BASIS,
    };
  }
  let headline: string;
  let tone: Tone = "neutral";
  switch (evaluation.ratio_state) {
    case "NOT_CONFIGURED":
      headline = explanations.has("CLASSROOM_INACTIVE")
        ? "Classroom inactive"
        : "No configured classroom policy in effect";
      break;
    case "INSUFFICIENT_DATA": {
      const availability = presence?.availability ?? (status.presence_connected ? "" : "PRESENCE_NOT_CONNECTED");
      if (availability === "PRESENCE_NOT_CONNECTED") {
        headline = "Presence counts not connected";
        details.push(
          "No presence has been reported for this classroom, so no ratio is calculated. " +
            "Camera occupancy is never used as a child or staff count.",
        );
      } else if (
        availability === "PRESENCE_STALE" ||
        availability === "PRESENCE_NOT_YET_VALID" ||
        explanations.has("CHILD_COUNT_STALE") ||
        explanations.has("STAFF_COUNT_STALE")
      ) {
        headline = "Presence data stale";
        details.push("The operator-reported count has expired. Report current presence to update.");
      } else if (availability === "PRESENCE_REVOKED") {
        headline = "Ratio data unavailable";
        details.push("The latest operator-reported count was withdrawn. No earlier report is used.");
      } else {
        headline = "Ratio data unavailable";
      }
      break;
    }
    case "NO_CHILDREN_PRESENT":
      headline = "No children reported as present";
      tone = "ok";
      break;
    case "WITHIN_CONFIGURED_POLICY":
      headline = "Within configured policy";
      tone = "ok";
      break;
    case "OVER_CONFIGURED_RATIO":
      headline = "Configured ratio exceeded";
      tone = "attention";
      break;
    case "OVER_CONFIGURED_GROUP_SIZE":
      headline = "Configured group size exceeded";
      tone = "attention";
      break;
    default:
      headline = "Ratio data unavailable";
  }
  if (evaluation.child_count !== null && evaluation.staff_count !== null) {
    details.push(`Children reported: ${evaluation.child_count}`);
    details.push(`Qualified staff reported: ${evaluation.staff_count}`);
  }
  if (evaluation.required_staff !== null) {
    details.push(`Required qualified staff: ${evaluation.required_staff}`);
  }
  if (evaluation.staff_deficit !== null && evaluation.child_count !== null) {
    details.push(`Staff deficit: ${evaluation.staff_deficit}`);
  }
  if (evaluation.conditions.includes("OVER_CONFIGURED_GROUP_SIZE") && evaluation.ratio_state !== "OVER_CONFIGURED_GROUP_SIZE") {
    details.push("The configured group size is also exceeded.");
  }
  if (presence?.availability === "PRESENCE_FRESH" && presence.valid_until) {
    details.push(`Source: Manual (operator-reported) · fresh until ${clock(presence.valid_until)}`);
  }
  const unexplained = status.reconciliation.unexplained_observed_people;
  if (unexplained) {
    details.push(
      `The camera sees ${unexplained} more ${unexplained === 1 ? "person" : "people"} than the ` +
        "reported presence accounts for. They are not counted as children or staff.",
    );
  }
  return { headline, tone, details, basis: BASIS };
}

export function policyNumbers(policy: RatioPolicy): string {
  const parts = [`Up to ${policy.max_children_per_staff} children per qualified staff member`];
  if (policy.minimum_staff > 0) parts.push(`at least ${policy.minimum_staff} qualified staff when children are present`);
  if (policy.maximum_group_size !== null) parts.push(`group size up to ${policy.maximum_group_size}`);
  return parts.join(" · ");
}

export function policyPeriod(policy: RatioPolicy): string {
  return policy.effective_through_date
    ? `${policy.effective_from_date} through ${policy.effective_through_date}`
    : `From ${policy.effective_from_date}, no end date`;
}

export function policyStatusLabel(policy: RatioPolicy): string {
  if (policy.status !== "ACTIVE") return "Deactivated";
  return policy.in_effect ? "In effect" : "Scheduled or ended";
}

export function cameraSummary(classroom: Classroom): string {
  const count = classroom.cameras.length;
  if (count === 0) return "No camera associated";
  return `${count} ${count === 1 ? "camera" : "cameras"}: ${classroom.cameras.map((camera) => camera.name).join(", ")}`;
}
