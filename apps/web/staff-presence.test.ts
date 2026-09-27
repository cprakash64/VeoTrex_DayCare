import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  parseManualPresence,
  type RatioStatus,
  ratioStatusView,
  validateManualPresence,
} from "./lib/classrooms";
import {
  COUNTS_LABEL,
  countedLabel,
  DEFAULT_LEASE_SECONDS,
  eligibilityLabel,
  formatRemaining,
  LEASE_CHOICES,
  NOT_COUNTED_LABEL,
  parseEligibilityCreate,
  parseEligibilityUpdate,
  parsePresenceMode,
  parseStaffPresence,
  rosterSummaryLines,
  sourceLabel,
  staffEntryView,
  staffErrorMessage,
  type StaffPresenceEntry,
} from "./lib/staff-presence";

const NOW = Date.parse("2026-09-25T16:00:00Z");
const STAFF = "44444444-4444-4444-8444-444444444444";

function entry(changes: Partial<StaffPresenceEntry> = {}): StaffPresenceEntry {
  return {
    staff_profile_id: STAFF,
    display_name: "Teacher A",
    staff_status: "ACTIVE",
    on_facility_roster: true,
    counts_toward_ratio: true,
    counted: true,
    state: "PRESENT",
    location: "HERE",
    other_classroom_id: null,
    other_classroom_name: null,
    checked_in_at: "2026-09-25T15:59:00Z",
    last_event_at: "2026-09-25T15:59:00Z",
    valid_until: "2026-09-25T16:14:00Z",
    ...changes,
  };
}

function rosterStatus(
  evaluation: Partial<RatioStatus["evaluation"]>,
  staffUntil: string | null = "2026-09-25T16:14:00Z",
): RatioStatus {
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
      evaluated_at: "2026-09-25T16:00:00Z",
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
    presence: {
      availability: "PRESENCE_FRESH",
      source: "MANUAL",
      snapshot_id: "22222222-2222-4222-8222-222222222222",
      observed_at: "2026-09-25T15:59:50Z",
      valid_until: "2026-09-25T16:01:50Z",
      freshness: "FRESH",
      child_count: 6,
      qualified_staff_count: null,
      visitor_count: 0,
    },
    presence_source_mode: "ROSTER_STAFF_PLUS_MANUAL_CHILDREN",
    sources: {
      mode: "ROSTER_STAFF_PLUS_MANUAL_CHILDREN",
      children: { count: 6, source: "MANUAL", freshness: "FRESH", valid_until: "2026-09-25T16:01:50Z" },
      qualified_staff: { count: 2, source: "STAFF_ROSTER", freshness: "FRESH", valid_until: staffUntil },
      visitors: { count: 0, source: "MANUAL", freshness: "FRESH", valid_until: "2026-09-25T16:01:50Z" },
      staff_roster: {
        source: "STAFF_ROSTER",
        count: 2,
        present: 3,
        present_ratio_ineligible: 1,
        present_inactive: 0,
        present_ambiguous: 0,
        stale: 0,
        freshness: "FRESH",
        valid_until: staffUntil,
        evaluated_at: "2026-09-25T16:00:00Z",
      },
    },
  };
}

describe("eligibility UI (V1-04C)", () => {
  it("uses configured-policy wording, never a qualification claim", () => {
    expect(eligibilityLabel(true)).toBe("Counts toward configured classroom policy");
    expect(eligibilityLabel(false)).toBe("Does not count toward configured classroom policy");
    for (const text of [COUNTS_LABEL, NOT_COUNTED_LABEL]) {
      for (const claim of ["legal", "licen", "certif", "qualified teacher", "compliant"]) {
        expect(text.toLowerCase()).not.toContain(claim);
      }
    }
  });

  it("the BFF parsers accept exactly a staff id, a boolean and an optional note", () => {
    const valid = { staff_profile_id: STAFF, counts_toward_ratio: true };
    expect(parseEligibilityCreate(valid)).toEqual({ ...valid, note: null });
    expect(parseEligibilityCreate({ ...valid, note: "  Owner plan  " })).toEqual({ ...valid, note: "Owner plan" });
    expect(parseEligibilityCreate({ ...valid, counts_toward_ratio: "yes" })).toBeNull();
    expect(parseEligibilityCreate({ ...valid, staff_profile_id: "not-a-uuid" })).toBeNull();
    expect(parseEligibilityCreate({ ...valid, note: "<b>x</b>" })).toBeNull();
    expect(parseEligibilityCreate({ ...valid, legally_qualified: true })).toBeNull();
    expect(parseEligibilityUpdate({ counts_toward_ratio: false })).toEqual({ counts_toward_ratio: false });
    expect(parseEligibilityUpdate({ counts_toward_ratio: false, staff_profile_id: STAFF })).toBeNull();
    expect(parsePresenceMode({ mode: "ROSTER_STAFF_PLUS_MANUAL_CHILDREN" })).not.toBeNull();
    expect(parsePresenceMode({ mode: "VISION" })).toBeNull();
    expect(parsePresenceMode({ mode: "STAFF_RECOGNITION" })).toBeNull();
  });
});

describe("check-in and check-out UI (V1-04C)", () => {
  it("check-in takes a staff id and a bounded lease only", () => {
    expect(parseStaffPresence({ staff_profile_id: STAFF, lease_seconds: 900 }, true)).toEqual({
      staff_profile_id: STAFF,
      lease_seconds: 900,
    });
    expect(parseStaffPresence({ staff_profile_id: STAFF }, true)).toEqual({ staff_profile_id: STAFF });
    for (const lease of [30, 4 * 3600 + 1, 86400, 900.5, "900"]) {
      expect(parseStaffPresence({ staff_profile_id: STAFF, lease_seconds: lease }, true)).toBeNull();
    }
    for (const extra of [{ face_match: STAFF }, { track_id: 1 }, { occurred_at: "2030-01-01" }, { image: "x" }]) {
      expect(parseStaffPresence({ staff_profile_id: STAFF, ...extra }, true)).toBeNull();
    }
  });

  it("check-out takes a staff id only", () => {
    expect(parseStaffPresence({ staff_profile_id: STAFF }, false)).toEqual({ staff_profile_id: STAFF });
    expect(parseStaffPresence({ staff_profile_id: STAFF, lease_seconds: 900 }, false)).toBeNull();
  });

  it("offers leases from 5 minutes to 4 hours with 15 minutes by default", () => {
    expect(DEFAULT_LEASE_SECONDS).toBe(900);
    expect(LEASE_CHOICES.map((choice) => choice.seconds)).toContain(DEFAULT_LEASE_SECONDS);
    expect(Math.max(...LEASE_CHOICES.map((choice) => choice.seconds))).toBe(4 * 3600);
  });

  it("controls follow the person's state", () => {
    const here = staffEntryView(entry(), NOW);
    expect([here.canCheckIn, here.canRefresh, here.canCheckOut]).toEqual([false, true, true]);
    const out = staffEntryView(entry({ state: "NOT_CHECKED_IN", location: "NONE", valid_until: null }), NOW);
    expect([out.canCheckIn, out.canRefresh, out.canCheckOut]).toEqual([true, false, false]);
    const elsewhere = staffEntryView(
      entry({ location: "OTHER_CLASSROOM", other_classroom_name: "Room 2" }),
      NOW,
    );
    expect(elsewhere.label).toBe("Checked in in Room 2");
    expect([elsewhere.canCheckIn, elsewhere.canCheckOut]).toEqual([true, false]);
    const inactive = staffEntryView(entry({ staff_status: "INACTIVE", state: "NOT_CHECKED_IN", location: "NONE" }), NOW);
    expect(inactive.canCheckIn).toBe(false);
    const unassigned = staffEntryView(entry({ on_facility_roster: false, state: "NOT_CHECKED_IN", location: "NONE" }), NOW);
    expect(unassigned.canCheckIn).toBe(false);
  });
});

describe("freshness countdown (V1-04C)", () => {
  it("counts down on the page's clock and reads expired at the lease end", () => {
    expect(staffEntryView(entry(), NOW).label).toBe("Checked in · expires in 14 min 0 s");
    expect(staffEntryView(entry(), Date.parse("2026-09-25T16:13:30Z")).label).toBe(
      "Checked in · expires in 30 s",
    );
    const expired = staffEntryView(entry(), Date.parse("2026-09-25T16:14:00Z"));
    expect(expired.state).toBe("expired");
    expect(expired.label).toContain("not counted");
    expect([expired.canRefresh, expired.canCheckIn, expired.canCheckOut]).toEqual([false, true, true]);
    expect(formatRemaining(3 * 3600 + 120)).toBe("3 h 2 min");
  });

  it("explains why a checked-in person did or did not count", () => {
    expect(countedLabel(entry())).toBe(COUNTS_LABEL);
    expect(countedLabel(entry({ counts_toward_ratio: false }))).toBe(NOT_COUNTED_LABEL);
    expect(countedLabel(entry({ staff_status: "INACTIVE" }))).toContain("not counted");
    expect(countedLabel(entry({ on_facility_roster: false }))).toContain("not counted");
  });
});

describe("ratio card provenance (V1-04C)", () => {
  it("says where every number came from", () => {
    const view = ratioStatusView(rosterStatus({}), NOW);
    expect(view.headline).toBe("Within configured policy");
    expect(view.details).toEqual(
      expect.arrayContaining([
        "Children reported: 6 — Manual",
        "Qualified staff present: 2 — Staff roster",
        "Required qualified staff: 2",
        "Checked in but not counted toward the configured ratio: 1",
      ]),
    );
    expect(sourceLabel("MANUAL")).toBe("Manual");
    expect(sourceLabel("STAFF_ROSTER")).toBe("Staff roster");
    expect(sourceLabel("STAFF_RECOGNITION")).toBe("Not connected");
  });

  it("an expired counted check-in hides the result until the server re-evaluates", () => {
    const view = ratioStatusView(rosterStatus({}, "2026-09-25T16:00:30Z"), Date.parse("2026-09-25T16:00:31Z"));
    expect(view.headline).toBe("Presence data stale");
    expect(view.details.join(" ")).not.toContain("Within");
  });

  it("the over-ratio case keeps configured wording", () => {
    const view = ratioStatusView(
      rosterStatus({
        ratio_state: "OVER_CONFIGURED_RATIO",
        staff_count: 1,
        staff_deficit: 1,
        explanations: ["STAFF_BELOW_REQUIRED"],
      }),
      NOW,
    );
    expect(view.headline).toBe("Configured ratio exceeded");
    expect(view.details).toContain("Staff deficit: 1");
    const text = `${view.headline} ${view.details.join(" ")}`.toLowerCase();
    for (const word of ["illegal", "violation", "compliant", "compliance", "law", "licensed", "certified"]) {
      expect(text).not.toContain(word);
    }
  });

  it("roster lines report ineligible, inactive and expired check-ins as not counted", () => {
    const lines = rosterSummaryLines({
      source: "STAFF_ROSTER",
      count: 1,
      present: 3,
      present_ratio_ineligible: 1,
      present_inactive: 1,
      present_ambiguous: 0,
      stale: 2,
      freshness: "FRESH",
      valid_until: null,
      evaluated_at: "2026-09-25T16:00:00Z",
    });
    expect(lines[0]).toBe("Qualified staff present: 1 — Staff roster");
    expect(lines.join(" ")).toContain("Expired check-ins (not counted): 2");
  });

  it("roster-mode manual reports carry children and visitors only", () => {
    const result = validateManualPresence(
      { child_count: "6", qualified_staff_count: "3", visitor_count: "1", valid_for_seconds: "120" },
      true,
    );
    expect(result).toEqual({ ok: true, value: { child_count: 6, visitor_count: 1, valid_for_seconds: 120 } });
    expect(parseManualPresence({ child_count: 6, visitor_count: 0, valid_for_seconds: 120 })).toEqual({
      child_count: 6,
      visitor_count: 0,
      valid_for_seconds: 120,
    });
    expect(parseManualPresence({ child_count: 6, visitor_count: 0, valid_for_seconds: 120, staff: 2 })).toBeNull();
    expect(staffErrorMessage("staff_count_comes_from_roster")).toContain("children and visitors only");
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

describe("staff roster UI source guarantees (V1-04C)", () => {
  const card = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/staff-presence-card.tsx"), "utf8");
  const panel = readFileSync(join(__dirname, "app/app/staff/[staffId]/staff-eligibility-panel.tsx"), "utf8");
  const files = [
    join(__dirname, "lib/staff-presence.ts"),
    join(__dirname, "app/app/classrooms/[classroomId]/staff-presence-card.tsx"),
    join(__dirname, "app/app/staff/[staffId]/staff-eligibility-panel.tsx"),
    ...sources(join(__dirname, "app/api/facilities")),
    ...sources(join(__dirname, "app/api/classrooms/[classroomId]/staff-presence")),
    join(__dirname, "app/api/classrooms/[classroomId]/presence-source-mode/route.ts"),
  ];

  it("claims no legal compliance or verified qualification", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const claim of [
        "legally qualified",
        "legally compliant",
        "state licensed",
        "state-licensed",
        "certified by veotrex",
        "licensed teacher",
        "compliant with",
        "arizona",
      ]) {
        expect(text, file).not.toContain(claim);
      }
    }
    expect(panel).toContain("Counts toward configured classroom policy");
    expect(panel).toContain("does not verify licences or qualifications");
  });

  it("shows adult staff names only and asks for no child identity", () => {
    for (const text of [card, panel]) {
      for (const field of ["child_name", "childname", "child_id", "photo", 'type="file"', "<img", "<video"]) {
        expect(text.toLowerCase()).not.toContain(field);
      }
    }
    // Every name rendered comes from a staff entry, an event about a staff profile or a facility.
    const rendered = [...card.matchAll(/\{([a-z]+)\.display_name\}/g)].map((match) => match[1]);
    expect(new Set(rendered)).toEqual(new Set(["entry", "event"]));
  });

  it("never checks anyone in from a camera, a face match or an unknown person", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/recognition-test|runRecognitionTest|face_match|occupancy|UNKNOWN/);
    }
    expect(card).toContain("nobody is ever checked in from a camera or a face match");
  });
});
