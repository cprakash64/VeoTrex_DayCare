import { describe, expect, it } from "vitest";

import {
  FACE_EVALUATION_ENV,
  faceEvaluationEnabled,
  recognitionPresentation,
  rejectionMessage,
  type RecognitionTestResult,
} from "./lib/staff";

function result(overrides: Partial<RecognitionTestResult> = {}): RecognitionTestResult {
  return {
    decision: "UNKNOWN",
    staff_id: null,
    display_name: null,
    score: 0.12,
    runner_up_score: null,
    reason: "below_threshold",
    candidates: 2,
    model_id: "yunet+sface",
    model_version: "2023mar+2021dec",
    threshold: 0.45,
    margin: 0.06,
    evaluation_only: true,
    ...overrides,
  };
}

describe("faceEvaluationEnabled", () => {
  it("is off when the variable is absent, so a deployment that never sets it is correct", () => {
    expect(faceEvaluationEnabled({})).toBe(false);
  });

  it("is on only for exactly 1", () => {
    expect(faceEvaluationEnabled({ [FACE_EVALUATION_ENV]: "1" })).toBe(true);
    expect(faceEvaluationEnabled({ [FACE_EVALUATION_ENV]: " 1 " })).toBe(true);
  });

  it.each(["0", "", "  ", "true", "yes", "TRUE", "on", "enabled"])(
    "is off for %o, so no truthy-looking value can switch it on by accident",
    (value) => {
      expect(faceEvaluationEnabled({ [FACE_EVALUATION_ENV]: value })).toBe(false);
    },
  );
});

describe("recognitionPresentation", () => {
  it("names the teacher on a match and says what it was compared against", () => {
    const view = recognitionPresentation(
      result({ decision: "MATCH", display_name: "Chandra", staff_id: "abc", score: 0.91 }),
    );
    expect(view.matched).toBe(true);
    expect(view.headline).toBe("Match: Chandra");
    expect(view.detail).toContain("similarity 0.910");
    expect(view.detail).toContain("2 enrolled teachers");
  });

  it("uses the singular when exactly one teacher is enrolled", () => {
    const view = recognitionPresentation(
      result({ decision: "MATCH", display_name: "Chandra", candidates: 1 }),
    );
    expect(view.detail).toContain("1 enrolled teacher.");
  });

  it("explains an unknown below the threshold without naming anyone", () => {
    const view = recognitionPresentation(result({ reason: "below_threshold" }));
    expect(view.matched).toBe(false);
    expect(view.headline).toBe("Unknown");
    expect(view.detail).toContain("not close enough");
  });

  it("explains an ambiguous unknown as resembling more than one teacher", () => {
    const view = recognitionPresentation(result({ reason: "ambiguous", score: 0.72 }));
    expect(view.detail).toContain("more than one enrolled teacher");
  });

  it("explains that nobody is enrolled rather than blaming the photo", () => {
    const view = recognitionPresentation(result({ reason: "no_candidates", candidates: 0 }));
    expect(view.detail).toContain("No teacher in this organization is enrolled");
  });

  it("falls back to a plain refusal for a reason it does not recognize", () => {
    const view = recognitionPresentation(result({ reason: "something_new" }));
    expect(view.headline).toBe("Unknown");
    expect(view.detail).toContain("not matched to an enrolled teacher");
  });

  it("never reveals the closest candidate when the decision was UNKNOWN", () => {
    // The API does not send a name with an UNKNOWN; even if a future one did, the wording must
    // not surface it - a withheld identity that leaks as a hint is the same disclosure.
    const view = recognitionPresentation(
      result({ reason: "ambiguous", display_name: "Chandra", staff_id: "abc" }),
    );
    expect(view.headline).toBe("Unknown");
    expect(view.detail).not.toContain("Chandra");
  });

  it("always shows the score and the threshold that produced the decision", () => {
    for (const decision of ["MATCH", "UNKNOWN"] as const) {
      const view = recognitionPresentation(
        result({ decision, display_name: decision === "MATCH" ? "Chandra" : null }),
      );
      expect(view.detail).toContain("evaluation threshold 0.45");
    }
  });
});

describe("rejection messages", () => {
  it("explains a blurry photo, the category the real detector adds", () => {
    expect(rejectionMessage("face_too_blurry")).toContain("too blurry");
  });

  it("still falls back safely for an unknown category", () => {
    expect(rejectionMessage("something_unexpected")).toBe("The photo was not accepted.");
    expect(rejectionMessage(undefined)).toBe("The photo was not accepted.");
  });
});
