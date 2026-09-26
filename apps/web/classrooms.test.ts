import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  apiErrorMessage,
  cameraSummary,
  type Classroom,
  parsePolicyPayload,
  type PolicyFormInput,
  policyNumbers,
  policyPeriod,
  type RatioPolicy,
  type RatioStatus,
  ratioStatusView,
  validateClassroomForm,
  validatePolicyForm,
} from "./lib/classrooms";

function form(overrides: Partial<PolicyFormInput> = {}): PolicyFormInput {
  return {
    label: "Toddler room policy",
    age_band_label: "Toddler",
    max_children_per_staff: "5",
    minimum_staff: "1",
    maximum_group_size: "10",
    effective_from_date: "2026-09-01",
    effective_through_date: "",
    source_reference: "Owner-provided",
    ...overrides,
  };
}

function status(overrides: Partial<RatioStatus["evaluation"]> = {}, extra: Partial<RatioStatus> = {}): RatioStatus {
  return {
    classroom_id: "11111111-1111-4111-8111-111111111111",
    evaluation: {
      ratio_state: "INSUFFICIENT_DATA",
      conditions: [],
      explanations: ["CHILD_COUNT_MISSING", "STAFF_COUNT_MISSING"],
      child_count: null,
      staff_count: null,
      max_children_per_staff: 5,
      minimum_staff: 1,
      required_staff: null,
      staff_deficit: null,
      group_size: null,
      maximum_group_size: 10,
      child_freshness: "MISSING",
      staff_freshness: "MISSING",
      evaluated_at: "2026-09-25T16:00:00Z",
      ...overrides,
    },
    reconciliation: {
      state: "NOT_AVAILABLE",
      authoritative_expected_people: null,
      vision_observed_people: null,
      unexplained_observed_people: null,
      unseen_expected_people: null,
      reasons: ["VISION_NOT_CONNECTED"],
    },
    presence_connected: false,
    vision_connected: false,
    policy_basis: "CONFIGURED_CLASSROOM_POLICY",
    ...extra,
  };
}

const POLICY: RatioPolicy = {
  policy_id: "22222222-2222-4222-8222-222222222222",
  label: "Toddler room policy",
  age_band_label: "Toddler",
  max_children_per_staff: 5,
  minimum_staff: 1,
  maximum_group_size: 10,
  effective_from: "2026-09-01T07:00:00+00:00",
  effective_until: null,
  effective_from_date: "2026-09-01",
  effective_through_date: null,
  status: "ACTIVE",
  revision: 1,
  source_reference: null,
  in_effect: true,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

describe("policy form validation (V1-04A)", () => {
  it("accepts a valid policy and converts it to exact integers", () => {
    const result = validatePolicyForm(form());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.max_children_per_staff).toBe(5);
      expect(result.value.minimum_staff).toBe(1);
      expect(result.value.maximum_group_size).toBe(10);
      expect(result.value.effective_through_date).toBeNull();
    }
  });

  it.each([
    ["max_children_per_staff", "0"],
    ["max_children_per_staff", "-3"],
    ["max_children_per_staff", "2.5"],
    ["max_children_per_staff", "101"],
    ["minimum_staff", "-1"],
    ["maximum_group_size", "0"],
    ["effective_from_date", "2026-02-30"],
    ["label", "<b>x</b>"],
    ["age_band_label", "<script>"],
  ] as const)("rejects %s = %s", (field, value) => {
    const result = validatePolicyForm(form({ [field]: value }));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.errors[field]).toBeTruthy();
  });

  it("rejects a last day before the first day", () => {
    const result = validatePolicyForm(form({ effective_through_date: "2026-08-31" }));
    expect(result.ok).toBe(false);
  });

  it("the BFF parser demands exact keys and integer numbers", () => {
    const valid = {
      label: "Policy",
      age_band_label: null,
      max_children_per_staff: 5,
      minimum_staff: 0,
      maximum_group_size: null,
      effective_from_date: "2026-09-01",
      effective_through_date: null,
      source_reference: null,
    };
    expect(parsePolicyPayload(valid)).not.toBeNull();
    expect(parsePolicyPayload({ ...valid, extra: 1 })).toBeNull();
    expect(parsePolicyPayload({ ...valid, max_children_per_staff: "5" })).toBeNull();
    expect(parsePolicyPayload({ ...valid, max_children_per_staff: 5.5 })).toBeNull();
    expect(parsePolicyPayload({ ...valid, max_children_per_staff: true })).toBeNull();
    expect(parsePolicyPayload([valid])).toBeNull();
  });

  it("classroom names and age bands are validated; blank age band clears", () => {
    expect(validateClassroomForm("Room 1", "")).toEqual({ ok: true, value: { name: "Room 1", age_band_label: null } });
    expect(validateClassroomForm("", "Toddler").ok).toBe(false);
    expect(validateClassroomForm("Room <1>", "").ok).toBe(false);
    expect(validateClassroomForm("Room 1", "<i>").ok).toBe(false);
  });

  it("maps API categories to plain messages and never echoes input", () => {
    expect(apiErrorMessage("policy_period_overlaps")).toContain("Another active policy");
    expect(apiErrorMessage("anything_else")).toContain("could not be saved");
  });
});

describe("ratio status wording (V1-04A)", () => {
  it("unconnected presence says so and never guesses", () => {
    const view = ratioStatusView(status());
    expect(view.headline).toBe("Presence counts not connected");
    expect(view.details.join(" ")).toContain("never used as a child or staff count");
    expect(view.tone).toBe("neutral");
  });

  it("an unavailable status is 'Ratio data unavailable'", () => {
    expect(ratioStatusView(null).headline).toBe("Ratio data unavailable");
  });

  it("stale presence is shown as stale", () => {
    const view = ratioStatusView(
      status({ explanations: ["CHILD_COUNT_STALE"] }, { presence_connected: true }),
    );
    expect(view.headline).toBe("Presence data stale");
  });

  it("within and exceeded use configured-policy wording", () => {
    expect(
      ratioStatusView(status({ ratio_state: "WITHIN_CONFIGURED_POLICY", child_count: 5, staff_count: 1, required_staff: 1, staff_deficit: 0 }, { presence_connected: true })).headline,
    ).toBe("Within configured policy");
    const over = ratioStatusView(
      status(
        { ratio_state: "OVER_CONFIGURED_RATIO", child_count: 6, staff_count: 1, required_staff: 2, staff_deficit: 1 },
        { presence_connected: true },
      ),
    );
    expect(over.headline).toBe("Configured ratio exceeded");
    expect(over.tone).toBe("attention");
    expect(over.details).toContain("Additional qualified staff needed: 1");
  });

  it("an unexplained person seen by the camera is never called a child", () => {
    const view = ratioStatusView(
      status(
        { ratio_state: "OVER_CONFIGURED_RATIO", child_count: 6, staff_count: 1, required_staff: 2, staff_deficit: 1 },
        {
          presence_connected: true,
          vision_connected: true,
          reconciliation: {
            state: "VISION_HIGHER_THAN_ROSTER",
            authoritative_expected_people: 7,
            vision_observed_people: 8,
            unexplained_observed_people: 1,
            unseen_expected_people: 0,
            reasons: [],
          },
        },
      ),
    );
    const text = view.details.join(" ");
    expect(text).toContain("Children recorded: 6");
    expect(text).toContain("1 more person than the recorded presence accounts for");
    expect(text).toContain("not counted as children or staff");
  });

  it("every headline is free of legal or compliance claims", () => {
    const states = [
      "NOT_CONFIGURED",
      "INSUFFICIENT_DATA",
      "NO_CHILDREN_PRESENT",
      "WITHIN_CONFIGURED_POLICY",
      "OVER_CONFIGURED_RATIO",
      "OVER_CONFIGURED_GROUP_SIZE",
    ];
    for (const ratio_state of states) {
      const view = ratioStatusView(status({ ratio_state }, { presence_connected: true }));
      const text = `${view.headline} ${view.details.join(" ")} ${view.basis}`.toLowerCase();
      for (const claim of ["compliant", "compliance", "legal", "law", "arizona", "certified", "violation"]) {
        expect(text).not.toContain(claim);
      }
      expect(view.basis).toBe("Configured classroom policy");
    }
  });

  it("policy and camera summaries read plainly", () => {
    expect(policyNumbers(POLICY)).toBe(
      "Up to 5 children per qualified staff member · at least 1 qualified staff when children are present · group size up to 10",
    );
    expect(policyPeriod(POLICY)).toBe("From 2026-09-01, no end date");
    const room = { cameras: [{ camera_id: "c", name: "Testing Indoor", status: "ACTIVE", zone_name: "Whole room" }] } as unknown as Classroom;
    expect(cameraSummary(room)).toBe("1 camera: Testing Indoor");
  });
});

function sources(root: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(root)) {
    if (entry.startsWith("._")) continue;
    const path = join(root, entry);
    if (statSync(path).isDirectory()) found.push(...sources(path));
    else if (/\.(ts|tsx)$/.test(entry)) found.push(path);
  }
  return found;
}

describe("classroom UI source guarantees (V1-04A)", () => {
  const files = [
    ...sources(join(__dirname, "app/app/classrooms")),
    ...sources(join(__dirname, "app/api/classrooms")),
    join(__dirname, "lib/classrooms.ts"),
    join(__dirname, "lib/classroom-routes.ts"),
  ];

  it("no classroom page claims legal compliance", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const claim of ["legally compliant", "state compliant", "arizona law", "certified compliant"]) {
        expect(text, file).not.toContain(claim);
      }
    }
  });

  it("no classroom code derives a child count from camera occupancy", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/child\w*\s*=\s*[^;\n]*(observed|vision|occupancy)[^;\n]*-/i);
      expect(text, file).not.toMatch(/UNKNOWN[^\n]*CHILD[^\n]*=/);
    }
  });
});
