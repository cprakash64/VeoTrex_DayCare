import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  MANUAL_LIMITS,
  parseManualPresence,
  type PresenceReport,
  presenceStateLabel,
  type RatioStatus,
  ratioStatusView,
  validateManualPresence,
} from "./lib/classrooms";

const NOW = Date.parse("2026-09-25T16:00:00Z");

function status(
  evaluation: Partial<RatioStatus["evaluation"]>,
  presence: Partial<NonNullable<RatioStatus["presence"]>>,
): RatioStatus {
  return {
    classroom_id: "11111111-1111-4111-8111-111111111111",
    evaluation: {
      ratio_state: "INSUFFICIENT_DATA",
      conditions: [],
      explanations: [],
      child_count: null,
      staff_count: null,
      max_children_per_staff: 5,
      minimum_staff: 0,
      required_staff: null,
      staff_deficit: null,
      group_size: null,
      maximum_group_size: null,
      child_freshness: "MISSING",
      staff_freshness: "MISSING",
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
    presence_connected: presence.availability !== "PRESENCE_NOT_CONNECTED",
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
      qualified_staff_count: 1,
      visitor_count: 0,
      ...presence,
    },
  };
}

const OVER = {
  ratio_state: "OVER_CONFIGURED_RATIO",
  conditions: ["OVER_CONFIGURED_RATIO"],
  explanations: ["STAFF_BELOW_REQUIRED"],
  child_count: 6,
  staff_count: 1,
  required_staff: 2,
  staff_deficit: 1,
  child_freshness: "FRESH",
  staff_freshness: "FRESH",
};

describe("manual presence form (V1-04B)", () => {
  it("accepts counts and a listed validity", () => {
    const result = validateManualPresence({
      child_count: "6",
      qualified_staff_count: "1",
      visitor_count: "",
      valid_for_seconds: "120",
    });
    expect(result).toEqual({
      ok: true,
      value: { child_count: 6, qualified_staff_count: 1, visitor_count: 0, valid_for_seconds: 120 },
    });
  });

  it.each([
    ["child_count", "-1"],
    ["child_count", "2.5"],
    ["child_count", String(MANUAL_LIMITS.children + 1)],
    ["qualified_staff_count", "-1"],
    ["qualified_staff_count", ""],
    ["visitor_count", "x"],
    ["valid_for_seconds", "86400"],
    ["valid_for_seconds", "0"],
  ] as const)("rejects %s = %s", (field, value) => {
    const result = validateManualPresence({
      child_count: "6",
      qualified_staff_count: "1",
      visitor_count: "0",
      valid_for_seconds: "120",
      [field]: value,
    });
    expect(result.ok).toBe(false);
  });

  it("the BFF parser refuses extra fields, strings, floats and names", () => {
    const valid = { child_count: 6, qualified_staff_count: 1, visitor_count: 0, valid_for_seconds: 120 };
    expect(parseManualPresence(valid)).toEqual(valid);
    expect(parseManualPresence({ ...valid, child_names: ["A"] })).toBeNull();
    expect(parseManualPresence({ ...valid, unknown_count: 2 })).toBeNull();
    expect(parseManualPresence({ ...valid, observed_at: "2030-01-01T00:00:00Z" })).toBeNull();
    expect(parseManualPresence({ ...valid, child_count: "6" })).toBeNull();
    expect(parseManualPresence({ ...valid, child_count: 6.5 })).toBeNull();
    expect(parseManualPresence({ ...valid, child_count: -1 })).toBeNull();
  });

  it("report state reads Fresh, Stale or Revoked on the page's own clock", () => {
    const report: PresenceReport = {
      snapshot_id: "33333333-3333-4333-8333-333333333333",
      source: "MANUAL",
      child_count: 6,
      qualified_staff_count: 1,
      visitor_count: 0,
      observed_at: "2026-09-25T15:59:50Z",
      valid_until: "2026-09-25T16:01:50Z",
      created_at: "2026-09-25T15:59:50Z",
      revoked_at: null,
      freshness: "FRESH",
      authoritative: true,
      submitted_by_caller: true,
    };
    expect(presenceStateLabel(report, NOW)).toBe("Fresh");
    expect(presenceStateLabel(report, Date.parse("2026-09-25T16:01:50Z"))).toBe("Stale");
    expect(presenceStateLabel({ ...report, revoked_at: "2026-09-25T16:00:00Z" }, NOW)).toBe("Revoked");
  });
});

describe("ratio card with manual presence (V1-04B)", () => {
  it("fresh: configured-policy wording with the numbers and the source", () => {
    const view = ratioStatusView(status(OVER, {}), NOW);
    expect(view.headline).toBe("Configured ratio exceeded");
    expect(view.basis).toBe("Configured classroom policy");
    expect(view.details).toEqual(
      expect.arrayContaining([
        "Children reported: 6",
        "Qualified staff reported: 1",
        "Required qualified staff: 2",
        "Staff deficit: 1",
      ]),
    );
    expect(view.details.join(" ")).toContain("Source: Manual (operator-reported)");
  });

  it("the previous result is not shown once the report expires on this clock", () => {
    const view = ratioStatusView(status(OVER, {}), Date.parse("2026-09-25T16:01:50Z"));
    expect(view.headline).toBe("Presence data stale");
    expect(view.details.join(" ")).not.toContain("Staff deficit");
  });

  it("stale from the server reads Presence data stale", () => {
    const view = ratioStatusView(
      status(
        { explanations: ["CHILD_COUNT_STALE", "STAFF_COUNT_STALE"] },
        { availability: "PRESENCE_STALE", freshness: "STALE", child_count: null, qualified_staff_count: null },
      ),
      NOW,
    );
    expect(view.headline).toBe("Presence data stale");
  });

  it("revoked reads Ratio data unavailable and never falls back", () => {
    const view = ratioStatusView(
      status(
        { explanations: ["CHILD_COUNT_MISSING", "STAFF_COUNT_MISSING"] },
        { availability: "PRESENCE_REVOKED", child_count: null, qualified_staff_count: null },
      ),
      NOW,
    );
    expect(view.headline).toBe("Ratio data unavailable");
    expect(view.details.join(" ")).toContain("No earlier report is used");
  });

  it("never says illegal, violation, non-compliant or names a state", () => {
    for (const evaluation of [OVER, { ratio_state: "WITHIN_CONFIGURED_POLICY" }, {}]) {
      const view = ratioStatusView(status(evaluation, {}), NOW);
      const text = `${view.headline} ${view.details.join(" ")}`.toLowerCase();
      for (const word of ["illegal", "violation", "compliant", "compliance", "law", "arizona"]) {
        expect(text).not.toContain(word);
      }
    }
  });
});

describe("manual presence UI source guarantees (V1-04B)", () => {
  const card = readFileSync(
    join(__dirname, "app/app/classrooms/[classroomId]/manual-presence-card.tsx"),
    "utf8",
  );

  it("asks for counts only: no name, identity or camera input", () => {
    const labels = [...card.matchAll(/label: "([^"]+)"/g)].map((match) => match[1]);
    expect(labels).toEqual(["Children present", "Qualified staff present", "Visitors present"]);
    for (const field of ["child_name", "staff_name", "photo", "image", 'type="file"']) {
      expect(card.toLowerCase()).not.toContain(field);
    }
    expect(card).not.toMatch(/\sname=/);
    // Every input id is one of the three counts or the validity choice - nothing from a camera.
    const ids = [...card.matchAll(/id=\{?[`"]presence-([^`"}]+)/g)].map((match) => match[1]);
    // "${field.key" is the templated id of the three count inputs (the scan stops at "}").
    expect(new Set(ids)).toEqual(new Set(["heading", "${field.key", "validity"]));
    expect(card.toLowerCase()).not.toMatch(/id=[^\n]*camera/);
    expect(card).toContain('min={0}');
    expect(card).toContain('type="number"');
  });

  it("labels the source as manual and never infers UNKNOWN people", () => {
    expect(card).toContain("Manual (operator-reported)");
    expect(card).not.toMatch(/UNKNOWN/);
  });
});
