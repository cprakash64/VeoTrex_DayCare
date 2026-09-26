import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  DEFAULT_DEADBAND,
  EMPTY_PORTAL_FORM,
  geometryProblem,
  MAX_DEADBAND,
  MAX_LABEL_LENGTH,
  MAX_PORTALS,
  MIN_PORTAL_LENGTH,
  MIN_SIDE_ALIGNMENT,
  parsePortalCreate,
  parsePortalUpdate,
  portalErrorMessage,
  sideLabel,
  validatePortalForm,
} from "./lib/portals";

const DOOR = { label: "Main door", x1: 0.5, y1: 0.05, x2: 0.5, y2: 0.95, inside_side: "RIGHT" };
const FORM = { ...EMPTY_PORTAL_FORM, label: "Main door", x1: "0.5", y1: "0.05", x2: "0.5", y2: "0.95", inside_side: "RIGHT" };

describe("camera doorway lines (V1-05A)", () => {
  it("accepts a vertical doorway with the room to one side", () => {
    expect(validatePortalForm(FORM)).toEqual({
      ok: true,
      value: { ...DOOR, inside_side: "RIGHT", deadband: DEFAULT_DEADBAND, enabled: true },
    });
    expect(geometryProblem(0.1, 0.8, 0.9, 0.8, "ABOVE")).toBeNull();
  });

  it("refuses short, out-of-frame, non-numeric and ambiguous lines", () => {
    expect(geometryProblem(0.5, 0.5, 0.5, 0.5, "RIGHT")).toMatch(/too short/);
    expect(geometryProblem(0.5, 0.1, 0.5, 1.5, "RIGHT")).toMatch(/between 0 and 1/);
    expect(geometryProblem(Number.NaN, 0.1, 0.5, 0.9, "RIGHT")).toMatch(/between 0 and 1/);
    expect(geometryProblem(Number.POSITIVE_INFINITY, 0.1, 0.5, 0.9, "RIGHT")).toMatch(/between 0 and 1/);
    expect(geometryProblem(0.5, 0.1, 0.5, 0.9, "ABOVE")).toMatch(/runs along the line/);
    expect(geometryProblem(0.1, 0.5, 0.9, 0.5, "LEFT")).toMatch(/runs along the line/);
    for (const bad of ["abc", "1.5", "-0.1", "NaN", "Infinity", ""]) {
      expect(validatePortalForm({ ...FORM, x1: bad }).ok).toBe(false);
    }
    expect(validatePortalForm({ ...FORM, inside_side: "" }).ok).toBe(false);
    expect(validatePortalForm({ ...FORM, deadband: "0.5" }).ok).toBe(false);
    expect(validatePortalForm({ ...FORM, label: "<b>door</b>" }).ok).toBe(false);
  });

  it("the room's side is never pre-selected", () => {
    expect(EMPTY_PORTAL_FORM.inside_side).toBe("");
    expect(validatePortalForm({ ...EMPTY_PORTAL_FORM, ...FORM, inside_side: "" }).ok).toBe(false);
  });

  it("the BFF accepts numbers, a side, a label and flags only", () => {
    expect(parsePortalCreate(DOOR)).toMatchObject({ ...DOOR, deadband: DEFAULT_DEADBAND, enabled: true });
    for (const extra of [{ track_id: 1 }, { image: "x" }, { face: [1] }, { person: "x" }]) {
      expect(parsePortalCreate({ ...DOOR, ...extra })).toBeNull();
    }
    expect(parsePortalCreate({ ...DOOR, x1: "0.5" })).toBeNull();
    expect(parsePortalCreate({ ...DOOR, x1: Number.NaN })).toBeNull();
    expect(parsePortalCreate({ ...DOOR, inside_side: "ABOVE" })).toBeNull();
    expect(parsePortalUpdate({ enabled: false })).toEqual({ enabled: false });
    expect(parsePortalUpdate({ x1: 1.2 })).toBeNull();
    expect(parsePortalUpdate({ enabled: "no" })).toBeNull();
    expect(parsePortalUpdate({})).toBeNull();
  });

  it("uses the same geometry rules as the API and the edge", () => {
    const api = readFileSync(join(__dirname, "../api/src/veotrex_api/camera_portal.py"), "utf8");
    const constant = (name: string) => Number(new RegExp(`^${name} = ([\\d.]+)$`, "m").exec(api)?.[1]);
    expect(constant("MAX_PORTALS")).toBe(MAX_PORTALS);
    expect(constant("MIN_PORTAL_LENGTH")).toBe(MIN_PORTAL_LENGTH);
    expect(constant("DEFAULT_DEADBAND")).toBe(DEFAULT_DEADBAND);
    expect(constant("MAX_DEADBAND")).toBe(MAX_DEADBAND);
    expect(constant("MIN_SIDE_ALIGNMENT")).toBe(MIN_SIDE_ALIGNMENT);
    expect(constant("MAX_LABEL_LENGTH")).toBe(MAX_LABEL_LENGTH);
  });

  it("describes sides and errors plainly", () => {
    expect(sideLabel("RIGHT")).toBe("Room is to the right of the line");
    expect(portalErrorMessage("portal_limit_reached")).toContain("at most 4");
    expect(portalErrorMessage("nope")).toBe("The change could not be saved. Try again.");
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

describe("doorway line UI source guarantees (V1-05A)", () => {
  const files = [
    join(__dirname, "lib/portals.ts"),
    ...sources(join(__dirname, "app/app/classrooms/[classroomId]/cameras")),
    ...sources(join(__dirname, "app/api/classrooms/[classroomId]/cameras")),
  ];

  it("has no image, upload, camera-capture or identity UI", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const forbidden of ["<img", "<video", "<canvas", 'type="file"', "FormData", "getUserMedia"]) {
        expect(text, file).not.toContain(forbidden);
      }
      // Shared CSS class names (e.g. the app-wide "staff-meta" style) are not wording.
      const prose = text.replace(/className="[^"]*"/g, "");
      const words = new Set(prose.toLowerCase().match(/[a-z]+/g) ?? []);
      for (const forbidden of ["teacher", "child", "children", "adult", "staff", "guardian", "biometric", "facial", "identity"]) {
        expect(words.has(forbidden), `${file}: ${forbidden}`).toBe(false);
      }
    }
  });

  it("says the configuration is not yet sent to cameras and that appearing is not entering", () => {
    const page = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/cameras/[cameraId]/page.tsx"), "utf8");
    const panel = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/cameras/[cameraId]/portal-panel.tsx"), "utf8");
    expect(panel).toContain("Not yet sent to cameras");
    expect(page).toContain("never counted as an entry or an exit");
    expect(page).toContain("nobody is identified");
  });
});
