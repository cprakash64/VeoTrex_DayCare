/**
 * Camera portals - doorway lines for anonymous room entry/exit (V1-05A).
 *
 * A portal is a line across a doorway in one camera's picture, with the room on one side. The
 * live edge runtime reports a person entering or leaving the room only when a track crosses such
 * a line; appearing in view is never an entry. Coordinates are normalised to the picture (0-1,
 * origin top-left, y down). The room's side is LEFT/RIGHT for a line running up and down the
 * picture and ABOVE/BELOW for one running across it; a side nearly parallel to the line is
 * ambiguous and refused - the same rules the API and the edge apply.
 *
 * Saving a portal here does not yet change what a camera reports: the edge does not receive
 * portals from the control plane in this stage. Each portal shows the exact local evaluation
 * flag instead. Nothing here concerns who anyone is.
 */

export type CameraPortal = Readonly<{
  portal_id: string;
  label: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  inside_side: string;
  inside_normal: ReadonlyArray<number>;
  deadband: number;
  enabled: boolean;
  status: string;
  revision: number;
  edge_flag: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}>;

export type CameraPortals = Readonly<{
  classroom_id: string;
  classroom_name: string;
  camera_id: string;
  camera_name: string;
  camera_status: string;
  can_configure: boolean;
  max_portals: number;
  edge_distribution: string;
  portals: ReadonlyArray<CameraPortal>;
}>;

export const MAX_PORTALS = 4;
export const MIN_PORTAL_LENGTH = 0.01;
export const DEFAULT_DEADBAND = 0.02;
export const MAX_DEADBAND = 0.1;
export const MIN_SIDE_ALIGNMENT = 0.5;
export const MAX_LABEL_LENGTH = 40;
export const LABEL_PATTERN = /^[A-Za-z0-9 _.()/:#-]*$/;
export const INSIDE_SIDES = ["LEFT", "RIGHT", "ABOVE", "BELOW"] as const;
export type InsideSide = (typeof INSIDE_SIDES)[number];

const SIDE_DIRECTIONS: Readonly<Record<InsideSide, readonly [number, number]>> = {
  LEFT: [-1, 0],
  RIGHT: [1, 0],
  ABOVE: [0, -1],
  BELOW: [0, 1],
};

export type PortalPayload = Readonly<{
  label: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  inside_side: InsideSide;
  deadband: number;
  enabled: boolean;
}>;

export type PortalForm = Readonly<{
  label: string;
  x1: string;
  y1: string;
  x2: string;
  y2: string;
  inside_side: string;
  deadband: string;
  enabled: boolean;
}>;

export const EMPTY_PORTAL_FORM: PortalForm = {
  label: "",
  x1: "",
  y1: "",
  x2: "",
  y2: "",
  inside_side: "",
  deadband: String(DEFAULT_DEADBAND),
  enabled: true,
};

function unit(text: string): number | null {
  if (!/^\s*(0(\.\d+)?|1(\.0+)?|\.\d+)\s*$/.test(text)) return null;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

export function cleanPortalLabel(value: string): string | null {
  const label = value.split(/\s+/).filter(Boolean).join(" ");
  if (!label || label.length > MAX_LABEL_LENGTH || !LABEL_PATTERN.test(label)) return null;
  return label;
}

/** The geometry rule a server would apply, or null when the line is acceptable. */
export function geometryProblem(x1: number, y1: number, x2: number, y2: number, side: string): string | null {
  if (![x1, y1, x2, y2].every((value) => Number.isFinite(value) && value >= 0 && value <= 1)) {
    return "Coordinates must be numbers between 0 and 1.";
  }
  if (!(INSIDE_SIDES as ReadonlyArray<string>).includes(side)) return "Choose which side is the room.";
  const dx = x2 - x1;
  const dy = y2 - y1;
  const length = Math.hypot(dx, dy);
  if (length < MIN_PORTAL_LENGTH) return "The line is too short (at least 0.01 of the picture).";
  const [sx, sy] = SIDE_DIRECTIONS[side as InsideSide];
  if (Math.abs((dy / length) * sx + (-dx / length) * sy) < MIN_SIDE_ALIGNMENT) {
    return "That side runs along the line. Use LEFT/RIGHT for a line running up and down the picture, ABOVE/BELOW for one running across it.";
  }
  return null;
}

export type PortalValidation =
  | Readonly<{ ok: true; value: PortalPayload }>
  | Readonly<{ ok: false; errors: Readonly<Record<string, string>> }>;

export function validatePortalForm(form: PortalForm): PortalValidation {
  const errors: Record<string, string> = {};
  const label = cleanPortalLabel(form.label);
  if (label === null) errors.label = "Name the doorway (up to 40 letters, digits, spaces or . _ - ( ) / : #).";
  const numbers = (["x1", "y1", "x2", "y2"] as const).map((key) => unit(form[key]));
  (["x1", "y1", "x2", "y2"] as const).forEach((key, index) => {
    if (numbers[index] === null) errors[key] = "A number between 0 and 1.";
  });
  const deadband = Number(form.deadband);
  if (!/^\s*\d*\.?\d+\s*$/.test(form.deadband) || !(deadband >= 0 && deadband <= MAX_DEADBAND)) {
    errors.deadband = `Between 0 and ${MAX_DEADBAND}.`;
  }
  if (!(INSIDE_SIDES as ReadonlyArray<string>).includes(form.inside_side)) {
    errors.inside_side = "Choose which side of the line is the room.";
  }
  if (Object.keys(errors).length === 0) {
    const [x1, y1, x2, y2] = numbers as number[];
    const problem = geometryProblem(x1, y1, x2, y2, form.inside_side);
    if (problem) errors.geometry = problem;
  }
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  const [x1, y1, x2, y2] = numbers as number[];
  return {
    ok: true,
    value: {
      label: label as string,
      x1,
      y1,
      x2,
      y2,
      inside_side: form.inside_side as InsideSide,
      deadband,
      enabled: form.enabled,
    },
  };
}

function record(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  return body as Record<string, unknown>;
}

const FIELDS = ["label", "x1", "y1", "x2", "y2", "inside_side", "deadband", "enabled"] as const;

/** Strict BFF re-validation of a create: numbers, a side, a label and flags - nothing else. */
export function parsePortalCreate(body: unknown): PortalPayload | null {
  const value = record(body);
  if (value === null || Object.keys(value).some((key) => !(FIELDS as ReadonlyArray<string>).includes(key))) return null;
  const { label, x1, y1, x2, y2, inside_side: side, deadband = DEFAULT_DEADBAND, enabled = true } = value;
  if (typeof label !== "string" || typeof side !== "string" || typeof enabled !== "boolean") return null;
  if (![x1, y1, x2, y2, deadband].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const clean = cleanPortalLabel(label);
  if (clean === null || geometryProblem(x1 as number, y1 as number, x2 as number, y2 as number, side) !== null) return null;
  if (!((deadband as number) >= 0 && (deadband as number) <= MAX_DEADBAND)) return null;
  return {
    label: clean,
    x1: x1 as number,
    y1: y1 as number,
    x2: x2 as number,
    y2: y2 as number,
    inside_side: side as InsideSide,
    deadband: deadband as number,
    enabled,
  };
}

export type PortalUpdatePayload = Partial<PortalPayload>;

/** A partial update; geometry is re-checked in full by the API against the stored portal. */
export function parsePortalUpdate(body: unknown): PortalUpdatePayload | null {
  const value = record(body);
  if (value === null || Object.keys(value).length === 0) return null;
  if (Object.keys(value).some((key) => !(FIELDS as ReadonlyArray<string>).includes(key))) return null;
  const payload: Record<string, unknown> = {};
  for (const key of ["x1", "y1", "x2", "y2"] as const) {
    if (!(key in value)) continue;
    const number = value[key];
    if (typeof number !== "number" || !Number.isFinite(number) || number < 0 || number > 1) return null;
    payload[key] = number;
  }
  if ("deadband" in value) {
    const number = value.deadband;
    if (typeof number !== "number" || !Number.isFinite(number) || number < 0 || number > MAX_DEADBAND) return null;
    payload.deadband = number;
  }
  if ("inside_side" in value) {
    if (typeof value.inside_side !== "string" || !(INSIDE_SIDES as ReadonlyArray<string>).includes(value.inside_side)) return null;
    payload.inside_side = value.inside_side;
  }
  if ("label" in value) {
    if (typeof value.label !== "string") return null;
    const clean = cleanPortalLabel(value.label);
    if (clean === null) return null;
    payload.label = clean;
  }
  if ("enabled" in value) {
    if (typeof value.enabled !== "boolean") return null;
    payload.enabled = value.enabled;
  }
  return payload as PortalUpdatePayload;
}

export function sideLabel(side: string): string {
  switch (side) {
    case "LEFT":
      return "Room is to the left of the line";
    case "RIGHT":
      return "Room is to the right of the line";
    case "ABOVE":
      return "Room is above the line";
    case "BELOW":
      return "Room is below the line";
    default:
      return side;
  }
}

const PORTAL_MESSAGES: Readonly<Record<string, string>> = {
  invalid_portal_label: "Name the doorway (up to 40 letters, digits, spaces or . _ - ( ) / : #).",
  invalid_portal_coordinates: "Coordinates must be numbers between 0 and 1.",
  invalid_portal_inside: "Choose which side of the line is the room.",
  invalid_portal_deadband: `The dead-band must be between 0 and ${MAX_DEADBAND}.`,
  portal_too_short: "The line is too short.",
  portal_inside_ambiguous: "That side runs along the line. Choose the other pair of sides.",
  portal_limit_reached: `A camera can have at most ${MAX_PORTALS} doorway lines.`,
  portal_label_exists: "Another doorway line on this camera already has that name.",
  portal_archived: "This doorway line is archived and cannot be changed.",
  camera_inactive: "This camera or classroom is not active.",
};

export function portalErrorMessage(category: string | null | undefined): string {
  return (category && PORTAL_MESSAGES[category]) || "The change could not be saved. Try again.";
}
