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

export type RatioStatus = Readonly<{
  classroom_id: string;
  evaluation: RatioEvaluation;
  reconciliation: VisionReconciliation;
  presence_connected: boolean;
  vision_connected: boolean;
  policy_basis: string;
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

export function ratioStatusView(status: RatioStatus | null): RatioStatusView {
  if (status === null) {
    return { headline: "Ratio data unavailable", tone: "neutral", details: [], basis: BASIS };
  }
  const { evaluation } = status;
  const details: string[] = [];
  const explanations = new Set(evaluation.explanations);
  let headline: string;
  let tone: Tone = "neutral";
  switch (evaluation.ratio_state) {
    case "NOT_CONFIGURED":
      headline = explanations.has("CLASSROOM_INACTIVE")
        ? "Classroom inactive"
        : "No configured classroom policy in effect";
      break;
    case "INSUFFICIENT_DATA":
      if (!status.presence_connected) {
        headline = "Presence counts not connected";
        details.push(
          "No attendance or staff presence source is connected yet, so no ratio is calculated. " +
            "Camera occupancy is never used as a child or staff count.",
        );
      } else if (explanations.has("CHILD_COUNT_STALE") || explanations.has("STAFF_COUNT_STALE")) {
        headline = "Presence data stale";
      } else {
        headline = "Ratio data unavailable";
      }
      break;
    case "NO_CHILDREN_PRESENT":
      headline = "No children recorded as present";
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
    details.push(
      `Children recorded: ${evaluation.child_count} · qualified staff recorded: ${evaluation.staff_count}`,
    );
  }
  if (evaluation.required_staff !== null) {
    details.push(`Qualified staff needed under the configured policy: ${evaluation.required_staff}`);
  }
  if (evaluation.staff_deficit) {
    details.push(`Additional qualified staff needed: ${evaluation.staff_deficit}`);
  }
  if (evaluation.conditions.includes("OVER_CONFIGURED_GROUP_SIZE") && evaluation.ratio_state !== "OVER_CONFIGURED_GROUP_SIZE") {
    details.push("The configured group size is also exceeded.");
  }
  const unexplained = status.reconciliation.unexplained_observed_people;
  if (unexplained) {
    details.push(
      `The camera sees ${unexplained} more ${unexplained === 1 ? "person" : "people"} than the ` +
        "recorded presence accounts for. They are not counted as children or staff.",
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
