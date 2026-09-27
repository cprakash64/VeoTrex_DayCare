/**
 * Guardian contacts, child associations and authorized release presentation (V1-04E).
 *
 * Pure functions over the API's own responses, plus the same input rules the API applies so
 * errors appear early. A guardian contact is an operator's roster entry for an adult - a display
 * name and an optional identifier - never a photo, a face, an identity document or a camera
 * track. The relationship label is the operator's own words and grants nothing: only an explicit
 * "Authorized for pickup" with a current period does. VeoTrex does not verify anyone's identity;
 * the verification method is the operator's statement.
 */

import { cleanDisplayName, cleanExternalReference } from "./children";
import { UUID_RE } from "./staff-presence";

export type GuardianSummary = Readonly<{
  guardian_contact_id: string;
  facility_id: string;
  display_name: string;
  status: string;
  external_reference: string | null;
  active_link_count: number;
  can_administer: boolean;
  created_at: string;
  updated_at: string;
}>;

export type FacilityGuardians = Readonly<{
  facility_id: string;
  facility_name: string;
  facility_timezone: string;
  can_administer: boolean;
  guardians: ReadonlyArray<GuardianSummary>;
}>;

export type ChildLink = Readonly<{
  link_id: string;
  child_profile_id: string;
  guardian_contact_id: string;
  guardian_display_name: string;
  guardian_status: string;
  relationship_label: string;
  pickup_authorized: boolean;
  effective_from: string;
  effective_until: string | null;
  status: string;
  note: string | null;
  revision: number;
  pickup_status: string;
  created_at: string;
  updated_at: string;
  deactivated_at: string | null;
}>;

export type ChildGuardians = Readonly<{
  child_profile_id: string;
  child_display_name: string;
  child_status: string;
  facility_id: string;
  facility_timezone: string;
  can_administer: boolean;
  evaluated_at: string;
  links: ReadonlyArray<ChildLink>;
}>;

export type ReleaseRecord = Readonly<{
  release_id: string;
  classroom_id: string;
  classroom_name: string;
  child_profile_id: string;
  guardian_contact_id: string;
  guardian_display_name: string;
  authorization_link_id: string;
  authorization_link_revision: number;
  verification_method: string;
  released_at: string;
  attendance_event_id: string;
  recorded_by_caller: boolean;
}>;

export type ReleaseHistory = Readonly<{ child_profile_id: string; releases: ReadonlyArray<ReleaseRecord> }>;

export type ReleaseCandidate = Readonly<{
  guardian_contact_id: string;
  display_name: string;
  relationship_label: string;
  link_id: string;
  effective_until: string | null;
}>;

export type UnavailableContact = Readonly<{
  guardian_contact_id: string;
  display_name: string;
  relationship_label: string;
  reason: string;
}>;

export type ChildReleaseOptions = Readonly<{
  child_profile_id: string;
  display_name: string;
  candidates: ReadonlyArray<ReleaseCandidate>;
  unavailable: ReadonlyArray<UnavailableContact>;
}>;

export type ReleaseOptions = Readonly<{
  classroom_id: string;
  can_release: boolean;
  evaluated_at: string;
  verification_methods: ReadonlyArray<string>;
  children: ReadonlyArray<ChildReleaseOptions>;
}>;

// ------------------------------------------------------------------------------ validation
export const RELATIONSHIP_LABEL_MAX = 64;
export const LINK_NOTE_MAX = 200;
const REFUSED_CHARACTERS = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u;

function cleanText(value: string, maximum: number): string | null {
  if (typeof value !== "string") return null;
  const normalised = value.normalize("NFC");
  if (REFUSED_CHARACTERS.test(normalised.trim())) return null;
  const text = normalised.split(/\s+/).filter(Boolean).join(" ");
  if (!text || [...text].length > maximum || text.includes("<") || text.includes(">")) return null;
  return text;
}

export function cleanRelationshipLabel(value: string): string | null {
  return cleanText(value, RELATIONSHIP_LABEL_MAX);
}

/** undefined = invalid; null = empty (no note). */
export function cleanLinkNote(value: string): string | null | undefined {
  if (!value.trim()) return null;
  return cleanText(value, LINK_NOTE_MAX) ?? undefined;
}

export type GuardianPayload = Readonly<{ display_name: string; external_reference: string | null }>;

export type GuardianValidation =
  | Readonly<{ ok: true; value: GuardianPayload }>
  | Readonly<{ ok: false; errors: Readonly<Record<string, string>> }>;

export function validateGuardianForm(displayName: string, externalReference: string): GuardianValidation {
  const errors: Record<string, string> = {};
  const name = cleanDisplayName(displayName);
  if (name === null) errors.display_name = "Enter a name staff will recognise (up to 120 characters, no < or >).";
  const reference = cleanExternalReference(externalReference);
  if (reference === undefined) errors.external_reference = "Letters, numbers and . _ : / - only (up to 64), or leave empty.";
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value: { display_name: name as string, external_reference: reference ?? null } };
}

function record(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

function onlyKeys(value: Record<string, unknown>, allowed: ReadonlyArray<string>): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

/** Strict BFF re-validation: a name and an optional identifier - nothing else about an adult. */
export function parseGuardianCreate(body: unknown): GuardianPayload | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, ["display_name", "external_reference"])) return null;
  if (typeof value.display_name !== "string") return null;
  const reference = value.external_reference;
  if (reference !== undefined && reference !== null && typeof reference !== "string") return null;
  const result = validateGuardianForm(value.display_name, (reference as string | null | undefined) ?? "");
  return result.ok ? result.value : null;
}

export type GuardianUpdatePayload = Readonly<{ display_name?: string; external_reference?: string | null }>;

export function parseGuardianUpdate(body: unknown): GuardianUpdatePayload | null {
  const value = record(body);
  if (value === null || Object.keys(value).length === 0) return null;
  if (!onlyKeys(value, ["display_name", "external_reference"])) return null;
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

// ---------------------------------------------------------------------------- facility time
const LOCAL_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;
const ISO_WITH_OFFSET = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$/;

function wallClock(utcMs: number, timeZone: string): Record<string, number> {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const values: Record<string, number> = {};
  for (const part of parts) if (part.type !== "literal") values[part.type] = Number(part.value);
  return values;
}

/** An instant -> "YYYY-MM-DDTHH:MM" on the facility's wall clock (for datetime-local inputs). */
export function utcToFacilityLocal(iso: string, timeZone: string): string {
  const clock = wallClock(Date.parse(iso), timeZone);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${clock.year}-${pad(clock.month)}-${pad(clock.day)}T${pad(clock.hour)}:${pad(clock.minute)}`;
}

/**
 * "YYYY-MM-DDTHH:MM" on the facility's wall clock -> an ISO instant with offset, or null when the
 * text is malformed or names a time that does not exist there (a daylight-saving gap).
 */
export function facilityLocalToUtc(local: string, timeZone: string): string | null {
  const match = LOCAL_PATTERN.exec(local);
  if (!match) return null;
  const [year, month, day, hour, minute] = match.slice(1).map(Number);
  const naive = Date.UTC(year, month - 1, day, hour, minute);
  if (Number.isNaN(naive)) return null;
  let guess = naive;
  for (let step = 0; step < 3; step += 1) {
    const clock = wallClock(guess, timeZone);
    const shown = Date.UTC(clock.year, clock.month - 1, clock.day, clock.hour, clock.minute);
    guess += naive - shown;
  }
  if (utcToFacilityLocal(new Date(guess).toISOString(), timeZone) !== local) return null;
  return new Date(guess).toISOString();
}

export function formatInFacility(iso: string | null, timeZone: string): string {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-US", { timeZone, dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

// ------------------------------------------------------------------------------------ links
export type LinkPayload = Readonly<{
  guardian_contact_id: string;
  relationship_label: string;
  pickup_authorized: boolean;
  effective_from?: string;
  effective_until?: string;
  note?: string;
}>;

export type LinkForm = Readonly<{
  guardian_contact_id: string;
  relationship_label: string;
  pickup: "" | "yes" | "no";
  effective_from: string; // facility-local datetime-local text, or ""
  effective_until: string;
  note: string;
}>;

export type LinkValidation =
  | Readonly<{ ok: true; value: LinkPayload }>
  | Readonly<{ ok: false; errors: Readonly<Record<string, string>> }>;

/** The add-association form. Nothing is pre-selected: the contact and the pickup decision are
 * both explicit choices, and the relationship label never implies either. */
export function validateLinkForm(form: LinkForm, timeZone: string): LinkValidation {
  const errors: Record<string, string> = {};
  if (!UUID_RE.test(form.guardian_contact_id)) errors.guardian_contact_id = "Choose a contact.";
  const label = cleanRelationshipLabel(form.relationship_label);
  if (label === null) errors.relationship_label = `Describe the relationship in up to ${RELATIONSHIP_LABEL_MAX} characters.`;
  if (form.pickup === "") errors.pickup = "Choose whether this person is authorized for pickup.";
  const from = form.effective_from ? facilityLocalToUtc(form.effective_from, timeZone) : undefined;
  const until = form.effective_until ? facilityLocalToUtc(form.effective_until, timeZone) : undefined;
  if (from === null) errors.effective_from = "Enter a valid start time.";
  if (until === null) errors.effective_until = "Enter a valid end time.";
  if (until && Date.parse(until) <= Date.parse(from ?? new Date().toISOString())) {
    errors.effective_until = "The end must be after the start.";
  }
  const note = cleanLinkNote(form.note);
  if (note === undefined) errors.note = `Up to ${LINK_NOTE_MAX} characters, without < or >.`;
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return {
    ok: true,
    value: {
      guardian_contact_id: form.guardian_contact_id,
      relationship_label: label as string,
      pickup_authorized: form.pickup === "yes",
      ...(from ? { effective_from: from } : {}),
      ...(until ? { effective_until: until } : {}),
      ...(note ? { note } : {}),
    },
  };
}

function validInstant(value: unknown): value is string {
  return typeof value === "string" && ISO_WITH_OFFSET.test(value) && !Number.isNaN(Date.parse(value));
}

export function parseLinkCreate(body: unknown): LinkPayload | null {
  const value = record(body);
  const allowed = ["guardian_contact_id", "relationship_label", "pickup_authorized", "effective_from", "effective_until", "note"];
  if (value === null || !onlyKeys(value, allowed)) return null;
  if (typeof value.guardian_contact_id !== "string" || !UUID_RE.test(value.guardian_contact_id)) return null;
  if (typeof value.relationship_label !== "string") return null;
  const label = cleanRelationshipLabel(value.relationship_label);
  if (label === null || typeof value.pickup_authorized !== "boolean") return null;
  const payload: {
    guardian_contact_id: string;
    relationship_label: string;
    pickup_authorized: boolean;
    effective_from?: string;
    effective_until?: string;
    note?: string;
  } = { guardian_contact_id: value.guardian_contact_id, relationship_label: label, pickup_authorized: value.pickup_authorized };
  for (const key of ["effective_from", "effective_until"] as const) {
    if (value[key] === undefined) continue;
    if (!validInstant(value[key])) return null;
    payload[key] = value[key] as string;
  }
  if (value.note !== undefined) {
    if (typeof value.note !== "string") return null;
    const note = cleanLinkNote(value.note);
    if (note === undefined) return null;
    if (note) payload.note = note;
  }
  return payload;
}

export type LinkUpdatePayload = Readonly<{
  relationship_label?: string;
  pickup_authorized?: boolean;
  effective_from?: string;
  effective_until?: string | null;
  note?: string | null;
}>;

export function parseLinkUpdate(body: unknown): LinkUpdatePayload | null {
  const value = record(body);
  const allowed = ["relationship_label", "pickup_authorized", "effective_from", "effective_until", "note"];
  if (value === null || Object.keys(value).length === 0 || !onlyKeys(value, allowed)) return null;
  const payload: {
    relationship_label?: string;
    pickup_authorized?: boolean;
    effective_from?: string;
    effective_until?: string | null;
    note?: string | null;
  } = {};
  if ("relationship_label" in value) {
    if (typeof value.relationship_label !== "string") return null;
    const label = cleanRelationshipLabel(value.relationship_label);
    if (label === null) return null;
    payload.relationship_label = label;
  }
  if ("pickup_authorized" in value) {
    if (typeof value.pickup_authorized !== "boolean") return null;
    payload.pickup_authorized = value.pickup_authorized;
  }
  if ("effective_from" in value) {
    if (!validInstant(value.effective_from)) return null;
    payload.effective_from = value.effective_from;
  }
  if ("effective_until" in value) {
    if (value.effective_until === null) payload.effective_until = null;
    else if (validInstant(value.effective_until)) payload.effective_until = value.effective_until;
    else return null;
  }
  if ("note" in value) {
    if (value.note === null) payload.note = null;
    else if (typeof value.note === "string") {
      const note = cleanLinkNote(value.note);
      if (note === undefined) return null;
      payload.note = note;
    } else return null;
  }
  return payload;
}

// ---------------------------------------------------------------------------------- release
export const VERIFICATION_METHODS: ReadonlyArray<{ method: string; label: string; help: string }> = [
  { method: "KNOWN_TO_STAFF", label: "Known to staff", help: "A staff member knows this adult." },
  { method: "OPERATOR_CONFIRMED", label: "Confirmed by me", help: "I confirmed who this adult is." },
  {
    method: "PHOTO_ID_CHECKED",
    label: "Photo ID checked",
    help: "I looked at an ID card. Nothing is scanned, copied or stored.",
  },
];

export type ReleasePayload = Readonly<{
  child_profile_id: string;
  guardian_contact_id: string;
  verification_method: string;
}>;

/** Exactly a child, an adult and a method - never a camera, a track, a note or an ID number. */
export function parseRelease(body: unknown): ReleasePayload | null {
  const value = record(body);
  if (value === null || !onlyKeys(value, ["child_profile_id", "guardian_contact_id", "verification_method"])) return null;
  const { child_profile_id: child, guardian_contact_id: adult, verification_method: method } = value;
  if (typeof child !== "string" || !UUID_RE.test(child)) return null;
  if (typeof adult !== "string" || !UUID_RE.test(adult)) return null;
  if (typeof method !== "string" || !VERIFICATION_METHODS.some((item) => item.method === method)) return null;
  return { child_profile_id: child, guardian_contact_id: adult, verification_method: method };
}

export type ReleaseSelection = Readonly<{ guardianContactId: string | null; method: string | null; confirmed: boolean }>;

export const EMPTY_RELEASE_SELECTION: ReleaseSelection = { guardianContactId: null, method: null, confirmed: false };

export type ReleaseReadiness =
  | Readonly<{ ready: true; payload: ReleasePayload }>
  | Readonly<{ ready: false; missing: ReadonlyArray<"person" | "method" | "confirmation"> }>;

/**
 * The release form is ready only when the operator has explicitly chosen one of the currently
 * authorized people, a verification method, and confirmed. Nothing defaults.
 */
export function releaseReadiness(
  childId: string,
  candidates: ReadonlyArray<ReleaseCandidate>,
  selection: ReleaseSelection,
): ReleaseReadiness {
  const missing: Array<"person" | "method" | "confirmation"> = [];
  const chosen = candidates.find((item) => item.guardian_contact_id === selection.guardianContactId);
  if (!chosen) missing.push("person");
  if (!selection.method || !VERIFICATION_METHODS.some((item) => item.method === selection.method)) missing.push("method");
  if (!selection.confirmed) missing.push("confirmation");
  if (missing.length > 0 || !chosen || !selection.method) return { ready: false, missing };
  return {
    ready: true,
    payload: { child_profile_id: childId, guardian_contact_id: chosen.guardian_contact_id, verification_method: selection.method },
  };
}

// ------------------------------------------------------------------------------ presentation
const PICKUP_REASONS: Readonly<Record<string, string>> = {
  AUTHORIZED: "Authorized for pickup",
  PICKUP_NOT_AUTHORIZED: "Not authorized for pickup",
  AUTHORIZATION_NOT_STARTED: "Not currently authorized · starts later",
  AUTHORIZATION_EXPIRED: "Not currently authorized · authorization ended",
  ASSOCIATION_INACTIVE: "Association ended",
  AUTHORIZED_PERSON_INACTIVE: "Not currently authorized · contact inactive",
  CHILD_INACTIVE: "Not currently authorized · child inactive",
  ASSOCIATION_AMBIGUOUS: "Not currently authorized · needs review",
  FACILITY_MISMATCH: "Not currently authorized",
  NO_ASSOCIATION: "Not currently authorized",
};

export function pickupStatusLabel(status: string): string {
  return PICKUP_REASONS[status] ?? "Not currently authorized";
}

export function guardianStatusLabel(status: string): string {
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

export function verificationLabel(method: string): string {
  return VERIFICATION_METHODS.find((item) => item.method === method)?.label ?? method;
}

const GUARDIAN_MESSAGES: Readonly<Record<string, string>> = {
  invalid_display_name: "Enter a name up to 120 characters, without < or > or invisible characters.",
  invalid_external_reference: "Letters, numbers and . _ : / - only (up to 64).",
  invalid_relationship_label: "Describe the relationship in up to 64 characters, without < or >.",
  invalid_link_note: "Keep the note under 200 characters, without < or >.",
  invalid_effective_period: "Enter valid start and end times.",
  effective_period_inverted: "The end must be after the start.",
  invalid_verification_method: "Choose how you confirmed this adult.",
  external_reference_exists: "Another contact at this facility already uses that reference.",
  guardian_limit_reached: "This facility has reached its contact limit.",
  guardian_archived: "This contact is archived and cannot be changed.",
  child_archived: "This child is archived and cannot be changed.",
  association_exists: "This contact is already associated with this child. Edit the existing entry instead.",
  association_limit_reached: "This child has reached the limit of associated contacts.",
  association_inactive: "This association has ended. Add a new one instead.",
  facility_mismatch: "This contact belongs to another facility.",
  facility_inactive: "This facility is not active.",
  no_association: "This adult is not associated with this child.",
  pickup_not_authorized: "This adult is not authorized for pickup.",
  authorization_not_started: "This adult's pickup authorization has not started yet.",
  authorization_expired: "This adult's pickup authorization has ended.",
  authorized_person_inactive: "This contact is inactive.",
  child_inactive: "This child is inactive. Use an administrative check-out if needed.",
  association_ambiguous: "This child's contacts need review before a release.",
  child_not_checked_in: "This child is not checked in here any more.",
  child_in_another_classroom: "This child is checked into another classroom.",
  attendance_expired: "Attendance has expired. Use an administrative check-out instead.",
  attendance_state_changed: "Someone else changed this at the same moment. Refresh and try again.",
  release_state_changed: "Someone else changed this at the same moment. Refresh and try again.",
};

export function guardianErrorMessage(category: string | null | undefined): string {
  return (category && GUARDIAN_MESSAGES[category]) || "The change could not be saved. Try again.";
}
