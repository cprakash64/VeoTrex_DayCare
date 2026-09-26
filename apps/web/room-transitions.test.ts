import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  cleanCursor,
  cleanTransitionType,
  MAX_TRANSITION_PAGE_SIZE,
  parseTransitionPage,
  TRANSITION_PAGE_SIZE,
  transitionQuery,
  transitionSentence,
} from "./lib/room-transitions";

const EVENT = {
  event_id: "6f1c1a52-8a2b-4a57-9d51-6e0f6b3e7b10",
  event_type: "ENTERED",
  occurred_at: "2026-09-26T15:04:05+00:00",
  received_at: "2026-09-26T15:04:06+00:00",
  camera_id: "0b9f8a3c-1d2e-4f50-8a6b-7c8d9e0f1a2b",
  camera_name: "Synthetic Indoor",
  portal_id: "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
  portal_label: "Main Door",
};
const PAGE = { classroom_id: "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", events: [EVENT], next_cursor: null };

describe("room transition timeline (V1-05B)", () => {
  it("says only that a person entered or exited via a doorway line", () => {
    expect(transitionSentence({ event_type: "ENTERED", portal_label: "Main Door" })).toBe(
      "Person entered via Main Door",
    );
    expect(transitionSentence({ event_type: "EXITED", portal_label: "Main Door" })).toBe(
      "Person exited via Main Door",
    );
  });

  it("keeps only the listed fields, whatever else a response carries", () => {
    const withExtras = {
      ...PAGE,
      events: [
        {
          ...EVENT,
          ephemeral_track_id: 7,
          stream_instance_id: "abc",
          display_name: "Someone",
          person_id: "p1",
          face: [1],
        },
      ],
    };
    const parsed = parseTransitionPage(withExtras);
    expect(parsed).not.toBeNull();
    expect(Object.keys(parsed!.events[0]).sort()).toEqual(
      ["camera_id", "camera_name", "event_id", "event_type", "occurred_at", "portal_id", "portal_label"].sort(),
    );
    expect(JSON.stringify(parsed)).not.toMatch(/track|stream|display_name|person|face/);
  });

  it("refuses malformed, unknown-type and oversized pages", () => {
    expect(parseTransitionPage(PAGE)).not.toBeNull();
    expect(parseTransitionPage(null)).toBeNull();
    expect(parseTransitionPage({ ...PAGE, events: "x" })).toBeNull();
    expect(parseTransitionPage({ ...PAGE, events: [{ ...EVENT, event_type: "LOITERED" }] })).toBeNull();
    expect(parseTransitionPage({ ...PAGE, events: [{ ...EVENT, occurred_at: "yesterday" }] })).toBeNull();
    expect(parseTransitionPage({ ...PAGE, events: [{ ...EVENT, portal_label: "" }] })).toBeNull();
    expect(parseTransitionPage({ ...PAGE, next_cursor: "a b" })).toBeNull();
    const many = Array.from({ length: MAX_TRANSITION_PAGE_SIZE + 1 }, () => EVENT);
    expect(parseTransitionPage({ ...PAGE, events: many })).toBeNull();
  });

  it("requests one bounded page with an opaque cursor and a simple type filter", () => {
    expect(TRANSITION_PAGE_SIZE).toBeLessThanOrEqual(MAX_TRANSITION_PAGE_SIZE);
    expect(transitionQuery(null, null)).toBe(`limit=${TRANSITION_PAGE_SIZE}`);
    expect(transitionQuery("abc_-1", "EXITED")).toBe(`limit=${TRANSITION_PAGE_SIZE}&cursor=abc_-1&event_type=EXITED`);
    expect(cleanCursor("abc")).toBe("abc");
    expect(cleanCursor("a/b")).toBeNull();
    expect(cleanCursor("x".repeat(129))).toBeNull();
    expect(cleanTransitionType("ENTERED")).toBe("ENTERED");
    expect(cleanTransitionType("entered")).toBeNull();
    expect(cleanTransitionType(["EXITED"])).toBeNull();
  });
});

function sources(root: string): string[] {
  const found: string[] = [];
  for (const name of readdirSync(root)) {
    if (name.startsWith("._")) continue;
    const path = join(root, name);
    if (statSync(path).isDirectory()) found.push(...sources(path));
    else if (/\.(ts|tsx)$/.test(name)) found.push(path);
  }
  return found;
}

describe("room transition UI source guarantees (V1-05B)", () => {
  const pageDir = join(__dirname, "app/app/classrooms/[classroomId]/room-transitions");
  const files = [join(__dirname, "lib/room-transitions.ts"), ...sources(pageDir)];
  const page = readFileSync(join(pageDir, "page.tsx"), "utf8");

  it("has no image, identity or person-matching wording or UI", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const forbidden of ["<img", "<video", "<canvas", 'type="file"', "dangerouslySetInnerHTML"]) {
        expect(text, file).not.toContain(forbidden);
      }
      const prose = text.replace(/className="[^"]*"/g, "");
      const words = new Set(prose.toLowerCase().match(/[a-z]+/g) ?? []);
      for (const forbidden of [
        "teacher",
        "child",
        "children",
        "staff",
        "guardian",
        "parent",
        "biometric",
        "facial",
        "identity",
        "recognized",
      ]) {
        expect(words.has(forbidden), `${file}: ${forbidden}`).toBe(false);
      }
    }
  });

  it("renders no track, stream or identity field", () => {
    for (const field of ["ephemeral_track_id", "track_id", "stream_instance_id", "display_name", "received_at"]) {
      expect(page).not.toContain(field);
    }
    expect(page).toContain("transitionSentence(event)");
    expect(page).toContain("Nobody is identified");
  });

  it("has loading, empty and error states and never fetches more than one page", () => {
    const loading = readFileSync(join(pageDir, "loading.tsx"), "utf8");
    expect(loading).toContain("Loading room transitions");
    expect(page).toContain("No room transitions have been reported for this classroom yet.");
    expect(page).toContain("Room transitions could not be loaded");
    expect(page.match(/getRoomTransitions\(/g)).toHaveLength(1);
    expect(page).not.toMatch(/while\s*\(|for\s*\(.*next_cursor/);
  });
});
