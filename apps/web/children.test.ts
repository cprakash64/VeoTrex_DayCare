import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  type AttendanceEntry,
  attendanceEntryView,
  attendanceSummaryLines,
  ATTENDANCE_LEASE_CHOICES,
  childErrorMessage,
  childStatusLabel,
  cleanDisplayName,
  DEFAULT_ATTENDANCE_LEASE_SECONDS,
  parseAttendance,
  parseChildCreate,
  parseChildUpdate,
  validateChildForm,
} from "./lib/children";
import { parseManualPresence, type RatioStatus, ratioStatusView, validateManualPresence } from "./lib/classrooms";
import { ATTENDANCE_MODE, parsePresenceMode, PRESENCE_MODE_CHOICES, sourceLabel } from "./lib/staff-presence";

const NOW = Date.parse("2026-09-26T15:00:00Z");
const CHILD = "55555555-5555-4555-8555-555555555555";

function entry(changes: Partial<AttendanceEntry> = {}): AttendanceEntry {
  return {
    child_profile_id: CHILD,
    display_name: "Child A",
    status: "ACTIVE",
    counted: true,
    state: "PRESENT",
    location: "HERE",
    other_classroom_id: null,
    other_classroom_name: null,
    checked_in_at: "2026-09-26T08:00:00Z",
    last_event_at: "2026-09-26T08:00:00Z",
    valid_until: "2026-09-26T20:00:00Z",
    ...changes,
  };
}

function attendanceStatus(evaluation: Partial<RatioStatus["evaluation"]>, visitors: number | null = null): RatioStatus {
  return {
    classroom_id: "11111111-1111-4111-8111-111111111111",
    evaluation: {
      ratio_state: "WITHIN_CONFIGURED_POLICY",
      conditions: [],
      explanations: ["WITHIN_CONFIGURED_POLICY"],
      child_count: 6,
      staff_count: 2,
      max_children_per_staff: 5,
      minimum_staff: 0,
      required_staff: 2,
      staff_deficit: 0,
      group_size: 6,
      maximum_group_size: null,
      child_freshness: "FRESH",
      staff_freshness: "FRESH",
      evaluated_at: "2026-09-26T15:00:00Z",
      ...evaluation,
    },
    reconciliation: {
      state: "NOT_AVAILABLE",
      authoritative_expected_people: null,
      vision_observed_people: null,
      unexplained_observed_people: null,
      unseen_expected_people: null,
      reasons: ["VISION_NOT_CONNECTED"],
    },
    presence_connected: true,
    vision_connected: false,
    policy_basis: "CONFIGURED_CLASSROOM_POLICY",
    // A visitor-only manual report that has already expired: it must not block the ratio.
    presence: {
      availability: "PRESENCE_FRESH",
      source: "MANUAL",
      snapshot_id: "22222222-2222-4222-8222-222222222222",
      observed_at: "2026-09-26T14:50:00Z",
      valid_until: "2026-09-26T14:52:00Z",
      freshness: "FRESH",
      child_count: null,
      qualified_staff_count: null,
      visitor_count: visitors,
    },
    presence_source_mode: ATTENDANCE_MODE,
    sources: {
      mode: ATTENDANCE_MODE,
      children: { count: 6, source: "ATTENDANCE", freshness: "FRESH", valid_until: "2026-09-26T20:00:00Z" },
      qualified_staff: { count: 2, source: "STAFF_ROSTER", freshness: "FRESH", valid_until: "2026-09-26T15:10:00Z" },
      visitors:
        visitors === null
          ? { count: null, source: null, freshness: "MISSING", valid_until: null }
          : { count: visitors, source: "MANUAL", freshness: "FRESH", valid_until: "2026-09-26T15:02:00Z" },
      staff_roster: {
        source: "STAFF_ROSTER",
        count: 2,
        present: 2,
        present_ratio_ineligible: 0,
        present_inactive: 0,
        present_ambiguous: 0,
        stale: 0,
        freshness: "FRESH",
        valid_until: "2026-09-26T15:10:00Z",
        evaluated_at: "2026-09-26T15:00:00Z",
      },
      child_attendance: {
        source: "ATTENDANCE",
        count: 6,
        present: 7,
        present_inactive: 1,
        stale: 1,
        freshness: "FRESH",
        valid_until: "2026-09-26T20:00:00Z",
        evaluated_at: "2026-09-26T15:00:00Z",
      },
    },
  };
}

describe("child roster form (V1-04D)", () => {
  it("normalises names and accepts an optional identifier", () => {
    expect(validateChildForm("  Child   A ", "SIS-1")).toEqual({
      ok: true,
      value: { display_name: "Child A", external_reference: "SIS-1" },
    });
    expect(validateChildForm("Child B", "")).toEqual({ ok: true, value: { display_name: "Child B", external_reference: null } });
    expect(cleanDisplayName("Zoe\u0308")).toBe("Zo\u00eb");
  });

  it.each([
    ["", ""],
    ["x".repeat(121), ""],
    ["Child\u0000A", ""],
    ["Child\tA", ""],
    ["Child\u200bA", ""],
    ["Child\u202eA", ""],
    ["<b>Child</b>", ""],
    ["Child A", "has space"],
    ["Child A", "-lead"],
  ])("rejects name %j / reference %j", (name, reference) => {
    expect(validateChildForm(name, reference).ok).toBe(false);
  });

  it("the BFF parsers accept a name and identifier only - never a photo, birth date or guardian", () => {
    expect(parseChildCreate({ display_name: "Child A" })).toEqual({ display_name: "Child A", external_reference: null });
    for (const extra of [{ photo: "x" }, { date_of_birth: "2022-01-01" }, { guardian_id: CHILD }, { face: [1] }]) {
      expect(parseChildCreate({ display_name: "Child A", ...extra })).toBeNull();
    }
    expect(parseChildUpdate({ external_reference: null })).toEqual({ external_reference: null });
    expect(parseChildUpdate({ display_name: "A\tB" })).toBeNull();
    expect(parseChildUpdate({})).toBeNull();
    expect(childStatusLabel("ARCHIVED")).toBe("Archived");
    expect(childErrorMessage("external_reference_exists")).toContain("already uses that reference");
  });
});

describe("attendance card (V1-04D)", () => {
  it("check-in, refresh and check-out take a child UUID and a bounded lease only", () => {
    expect(parseAttendance({ child_profile_id: CHILD, lease_seconds: 43200 }, true)).toEqual({
      child_profile_id: CHILD,
      lease_seconds: 43200,
    });
    for (const lease of [60, 1799, 43201, 64800, "3600"]) {
      expect(parseAttendance({ child_profile_id: CHILD, lease_seconds: lease }, true)).toBeNull();
    }
    for (const extra of [{ track_id: 3 }, { camera_id: CHILD }, { face_match: CHILD }]) {
      expect(parseAttendance({ child_profile_id: CHILD, ...extra }, true)).toBeNull();
    }
    expect(parseAttendance({ track_id: 3 }, true)).toBeNull();
    expect(parseAttendance({ child_profile_id: CHILD, lease_seconds: 3600 }, false)).toBeNull();
    expect(DEFAULT_ATTENDANCE_LEASE_SECONDS).toBe(12 * 3600);
    expect(Math.max(...ATTENDANCE_LEASE_CHOICES.map((choice) => choice.seconds))).toBe(12 * 3600);
  });

  it("controls follow the child's state: check in, move, refresh, check out", () => {
    const here = attendanceEntryView(entry(), NOW);
    expect([here.canCheckIn, here.canRefresh, here.canCheckOut]).toEqual([false, true, true]);
    expect(here.label).toBe("Present · expires in 5 h");
    const out = attendanceEntryView(entry({ state: "NOT_CHECKED_IN", location: "NONE", valid_until: null }), NOW);
    expect([out.canCheckIn, out.canRefresh, out.canCheckOut]).toEqual([true, false, false]);
    const elsewhere = attendanceEntryView(entry({ location: "OTHER_CLASSROOM", other_classroom_name: "Room Y" }), NOW);
    expect(elsewhere.label).toBe("Present in Room Y");
    expect([elsewhere.canCheckIn, elsewhere.canCheckOut]).toEqual([true, false]);
    const archived = attendanceEntryView(entry({ status: "ARCHIVED" }), NOW);
    expect(archived.label).toContain("not counted");
    expect([archived.canCheckIn, archived.canRefresh, archived.canCheckOut]).toEqual([false, false, true]);
  });

  it("an attendance that expires on this clock reads expired and can only be re-checked-in", () => {
    const expired = attendanceEntryView(entry(), Date.parse("2026-09-26T20:00:00Z"));
    expect(expired.state).toBe("expired");
    expect([expired.canCheckIn, expired.canRefresh, expired.canCheckOut]).toEqual([true, false, true]);
  });
});

describe("attendance mode (V1-04D)", () => {
  it("the manual form asks for visitors only and never assumes zero", () => {
    const form = { child_count: "6", qualified_staff_count: "2", visitor_count: "1", valid_for_seconds: "120" };
    expect(validateManualPresence(form, false, true)).toEqual({ ok: true, value: { visitor_count: 1, valid_for_seconds: 120 } });
    expect(validateManualPresence({ ...form, visitor_count: "" }, false, true).ok).toBe(false);
    expect(parseManualPresence({ visitor_count: 0, valid_for_seconds: 120 })).toEqual({ visitor_count: 0, valid_for_seconds: 120 });
    expect(parseManualPresence({ qualified_staff_count: 1, visitor_count: 0, valid_for_seconds: 120 })).toBeNull();
  });

  it("the ratio card shows Attendance and Staff roster provenance", () => {
    const view = ratioStatusView(attendanceStatus({}), NOW);
    expect(view.headline).toBe("Within configured policy");
    expect(view.details).toEqual(
      expect.arrayContaining([
        "Children present: 6 — Attendance",
        "Qualified staff present: 2 — Staff roster",
        "Required qualified staff: 2",
        "Expired attendance (not counted): 1",
        "Visitors: not reported (not used for the configured ratio)",
      ]),
    );
    expect(sourceLabel("ATTENDANCE")).toBe("Attendance");
    expect(attendanceSummaryLines(attendanceStatus({}).sources!.child_attendance!)[0]).toBe("Children present: 6 — Attendance");
  });

  it("an expired visitor report never blocks the ratio in attendance mode", () => {
    const view = ratioStatusView(attendanceStatus({}, 2), NOW);
    expect(view.headline).toBe("Within configured policy");
    expect(view.details).toContain("Visitors reported: 2 — Manual");
  });

  it("an expired counted attendance hides the result until re-evaluated", () => {
    const view = ratioStatusView(attendanceStatus({}), Date.parse("2026-09-26T20:00:01Z"));
    expect(view.headline).toBe("Presence data stale");
  });

  it("the source selector offers the three explicit modes and no camera source", () => {
    expect(PRESENCE_MODE_CHOICES.map((choice) => choice.mode)).toEqual([
      "MANUAL_AGGREGATE",
      "ROSTER_STAFF_PLUS_MANUAL_CHILDREN",
      "ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF",
    ]);
    expect(parsePresenceMode({ mode: ATTENDANCE_MODE })).not.toBeNull();
    for (const mode of ["VISION", "CAMERA", "FACE_RECOGNITION"]) expect(parsePresenceMode({ mode })).toBeNull();
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

describe("child UI source guarantees (V1-04D)", () => {
  const files = [
    join(__dirname, "lib/children.ts"),
    ...sources(join(__dirname, "app/app/children")),
    ...sources(join(__dirname, "app/api/children")),
    ...sources(join(__dirname, "app/api/facilities/[facilityId]/children")),
    ...sources(join(__dirname, "app/api/classrooms/[classroomId]/attendance")),
    join(__dirname, "app/app/classrooms/[classroomId]/child-attendance-card.tsx"),
    join(__dirname, "app/app/classrooms/[classroomId]/presence-source-control.tsx"),
  ];
  const card = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/child-attendance-card.tsx"), "utf8");
  const manual = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/manual-presence-card.tsx"), "utf8");

  it("has no child image, photo, face or biometric UI", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const forbidden of ["<img", "<video", 'type="file"', "FormData", "getUserMedia", "date_of_birth", "dateOfBirth"]) {
        expect(text, file).not.toContain(forbidden);
      }
      // V1-04E added a non-biometric guardian association (ids, names, labels and dates only), so
      // guardian identifiers are allowed here; guardians.test.ts pins what that UI may not do.
      expect(text, file).not.toMatch(/face_|faceMatch|photo_|embedding[_A-Z]|biometric[_A-Z]|recognition-test|runRecognitionTest|track_id/);
    }
  });

  it("claims no legal compliance", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const claim of ["legally compliant", "state compliant", "compliant with", "licensed", "arizona", "violation"]) {
        expect(text, file).not.toContain(claim);
      }
    }
  });

  it("names are rendered as text from roster entries only", () => {
    expect(card).not.toContain("dangerouslySetInnerHTML");
    const rendered = [...card.matchAll(/\{([a-z]+)\.display_name\}/g)].map((match) => match[1]);
    expect(new Set(rendered)).toEqual(new Set(["entry", "event"]));
    expect(card).toContain("cameras never check a child in or out");
  });

  it("attendance mode removes the manual child and staff inputs and says where counts come from", () => {
    expect(manual).toContain('!(attendanceMode && field.key === "child_count")');
    expect(manual).toContain('!((rosterMode || attendanceMode) && field.key === "qualified_staff_count")');
    for (const line of ["Children: Attendance", "Qualified staff: Staff roster", "Visitors: Manual"]) {
      expect(manual).toContain(line);
    }
  });
});
