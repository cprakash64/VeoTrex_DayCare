/**
 * Room transitions - the anonymous entry/exit timeline of one classroom (V1-05B).
 *
 * An edge node reports a room transition when an anonymous camera-session track crosses a
 * doorway line. The timeline says exactly that and nothing more: "Person entered via Main Door".
 * Nobody is identified, an entry is never paired with an exit, and no number or name the edge
 * used for a track reaches this page - the API does not return one, and the parser below keeps
 * only the fields listed here even if a response ever carried more.
 */

export const TRANSITION_PAGE_SIZE = 50;
export const MAX_TRANSITION_PAGE_SIZE = 200;
export const CURSOR_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
export const TRANSITION_TYPES = ["ENTERED", "EXITED"] as const;
export type TransitionType = (typeof TRANSITION_TYPES)[number];

export type RoomTransition = Readonly<{
  event_id: string;
  event_type: TransitionType;
  occurred_at: string;
  camera_id: string;
  camera_name: string;
  portal_id: string;
  portal_label: string;
}>;

export type RoomTransitionPage = Readonly<{
  classroom_id: string;
  events: ReadonlyArray<RoomTransition>;
  next_cursor: string | null;
}>;

export type RoomTransitionResult =
  | Readonly<{ status: "ok"; page: RoomTransitionPage }>
  | Readonly<{ status: "not_found" }>
  | Readonly<{ status: "error" }>;

function text(value: unknown, max: number): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= max ? value : null;
}

function transition(value: unknown): RoomTransition | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const type = item.event_type;
  if (type !== "ENTERED" && type !== "EXITED") return null;
  const occurred = text(item.occurred_at, 64);
  if (occurred === null || Number.isNaN(Date.parse(occurred))) return null;
  const fields = {
    event_id: text(item.event_id, 36),
    camera_id: text(item.camera_id, 36),
    camera_name: text(item.camera_name, 200),
    portal_id: text(item.portal_id, 36),
    portal_label: text(item.portal_label, 40),
  };
  if (Object.values(fields).some((field) => field === null)) return null;
  // Rebuilt field by field: nothing the API adds later can reach the page by accident.
  return {
    event_id: fields.event_id as string,
    event_type: type,
    occurred_at: occurred,
    camera_id: fields.camera_id as string,
    camera_name: fields.camera_name as string,
    portal_id: fields.portal_id as string,
    portal_label: fields.portal_label as string,
  };
}

/** Strict, bounded parse of one API page. Null when anything is out of shape. */
export function parseTransitionPage(body: unknown): RoomTransitionPage | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const value = body as Record<string, unknown>;
  const classroom = text(value.classroom_id, 36);
  const cursor = value.next_cursor;
  if (classroom === null || !Array.isArray(value.events)) return null;
  if (value.events.length > MAX_TRANSITION_PAGE_SIZE) return null;
  if (cursor !== null && (typeof cursor !== "string" || !CURSOR_PATTERN.test(cursor))) return null;
  const events: RoomTransition[] = [];
  for (const item of value.events) {
    const parsed = transition(item);
    if (parsed === null) return null;
    events.push(parsed);
  }
  return { classroom_id: classroom, events, next_cursor: cursor as string | null };
}

/** The only sentence a transition is ever shown as. The label is rendered as text. */
export function transitionSentence(event: Pick<RoomTransition, "event_type" | "portal_label">): string {
  return `Person ${event.event_type === "ENTERED" ? "entered" : "exited"} via ${event.portal_label}`;
}

export function cleanCursor(value: unknown): string | null {
  return typeof value === "string" && CURSOR_PATTERN.test(value) ? value : null;
}

export function cleanTransitionType(value: unknown): TransitionType | null {
  return value === "ENTERED" || value === "EXITED" ? value : null;
}

/** Query string for one page request: a bounded size, an optional opaque cursor and type. */
export function transitionQuery(cursor: string | null, type: TransitionType | null): string {
  const query = new URLSearchParams({ limit: String(TRANSITION_PAGE_SIZE) });
  if (cursor !== null) query.set("cursor", cursor);
  if (type !== null) query.set("event_type", type);
  return query.toString();
}
