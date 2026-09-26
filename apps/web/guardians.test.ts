import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  EMPTY_RELEASE_SELECTION,
  facilityLocalToUtc,
  formatInFacility,
  guardianErrorMessage,
  parseGuardianCreate,
  parseGuardianUpdate,
  parseLinkCreate,
  parseLinkUpdate,
  parseRelease,
  pickupStatusLabel,
  type ReleaseCandidate,
  releaseReadiness,
  utcToFacilityLocal,
  validateGuardianForm,
  validateLinkForm,
  VERIFICATION_METHODS,
} from "./lib/guardians";

const CHILD = "55555555-5555-4555-8555-555555555555";
const ADULT = "66666666-6666-4666-8666-666666666666";
const OTHER = "77777777-7777-4777-8777-777777777777";
const CANDIDATES: ReadonlyArray<ReleaseCandidate> = [
  { guardian_contact_id: ADULT, display_name: "Adult P1", relationship_label: "Mother", link_id: OTHER, effective_until: null },
];

describe("guardian contacts (V1-04E)", () => {
  it("accepts a name and an optional identifier only", () => {
    expect(validateGuardianForm("  Adult   Maya ", " FAM-1 ")).toEqual({
      ok: true,
      value: { display_name: "Adult Maya", external_reference: "FAM-1" },
    });
    expect(validateGuardianForm("", "").ok).toBe(false);
    expect(validateGuardianForm("<b>x</b>", "").ok).toBe(false);
    expect(validateGuardianForm("x".repeat(121), "").ok).toBe(false);
    expect(parseGuardianCreate({ display_name: "Adult A" })).toEqual({ display_name: "Adult A", external_reference: null });
    for (const extra of [
      { photo: "x" },
      { face_embedding: [1] },
      { id_document_image: "x" },
      { government_id_number: "123" },
      { date_of_birth: "1990-01-01" },
      { phone: "+15555550100" },
      { email: "a@example.test" },
      { track_id: 3 },
    ]) {
      expect(parseGuardianCreate({ display_name: "Adult A", ...extra })).toBeNull();
    }
    expect(parseGuardianUpdate({ external_reference: null })).toEqual({ external_reference: null });
    expect(parseGuardianUpdate({})).toBeNull();
    expect(parseGuardianUpdate({ photo: "x" })).toBeNull();
  });
});

describe("child associations (V1-04E)", () => {
  const form = {
    guardian_contact_id: ADULT,
    relationship_label: " Family  friend ",
    pickup: "yes" as const,
    effective_from: "2026-09-26T08:00",
    effective_until: "2026-09-26T18:00",
    note: "",
  };

  it("needs an explicit contact and an explicit pickup decision", () => {
    const empty = validateLinkForm({ ...form, guardian_contact_id: "", pickup: "" }, "America/Phoenix");
    expect(empty.ok).toBe(false);
    if (!empty.ok) expect(Object.keys(empty.errors).sort()).toEqual(["guardian_contact_id", "pickup"]);
  });

  it("keeps the relationship label and the pickup flag independent", () => {
    const father = validateLinkForm({ ...form, relationship_label: "Father", pickup: "no" }, "America/Phoenix");
    expect(father).toMatchObject({ ok: true, value: { relationship_label: "Father", pickup_authorized: false } });
    const friend = validateLinkForm(form, "America/Phoenix");
    expect(friend).toMatchObject({ ok: true, value: { relationship_label: "Family friend", pickup_authorized: true } });
  });

  it("converts temporary authorization times from facility time", () => {
    const result = validateLinkForm(form, "America/Phoenix");
    expect(result).toMatchObject({
      ok: true,
      value: { effective_from: "2026-09-26T15:00:00.000Z", effective_until: "2026-09-27T01:00:00.000Z" },
    });
    expect(validateLinkForm({ ...form, effective_until: "2026-09-26T07:00" }, "America/Phoenix").ok).toBe(false);
    expect(facilityLocalToUtc("2026-07-01T09:30", "America/New_York")).toBe("2026-07-01T13:30:00.000Z");
    expect(facilityLocalToUtc("2026-12-01T09:30", "America/New_York")).toBe("2026-12-01T14:30:00.000Z");
    expect(facilityLocalToUtc("2026-03-08T02:30", "America/New_York")).toBeNull(); // does not exist
    expect(facilityLocalToUtc("2026-09-26 08:00", "UTC")).toBeNull();
    expect(utcToFacilityLocal("2026-09-26T15:00:00Z", "America/Phoenix")).toBe("2026-09-26T08:00");
    expect(formatInFacility("2026-09-26T15:00:00Z", "America/Phoenix")).toContain("8:00");
  });

  it("the BFF accepts ids, a label, an explicit boolean and offset times only", () => {
    const body = { guardian_contact_id: ADULT, relationship_label: "Grandparent", pickup_authorized: true };
    expect(parseLinkCreate(body)).toEqual(body);
    expect(parseLinkCreate({ ...body, pickup_authorized: "yes" })).toBeNull();
    expect(parseLinkCreate({ guardian_contact_id: ADULT, relationship_label: "Mother" })).toBeNull();
    expect(parseLinkCreate({ ...body, effective_until: "2026-09-26T18:00" })).toBeNull();
    expect(parseLinkCreate({ ...body, effective_until: "2026-09-26T18:00:00Z" })).not.toBeNull();
    for (const extra of [{ kinship_score: 0.9 }, { face_match_id: OTHER }, { photo: "x" }, { track_id: 1 }]) {
      expect(parseLinkCreate({ ...body, ...extra })).toBeNull();
    }
    expect(parseLinkUpdate({ pickup_authorized: false })).toEqual({ pickup_authorized: false });
    expect(parseLinkUpdate({ effective_until: null, note: null })).toEqual({ effective_until: null, note: null });
    expect(parseLinkUpdate({ pickup_authorized: 0 })).toBeNull();
    expect(parseLinkUpdate({ guardian_contact_id: ADULT })).toBeNull();
  });

  it("labels say authorized or not, calmly", () => {
    expect(pickupStatusLabel("AUTHORIZED")).toBe("Authorized for pickup");
    expect(pickupStatusLabel("PICKUP_NOT_AUTHORIZED")).toBe("Not authorized for pickup");
    expect(pickupStatusLabel("AUTHORIZATION_EXPIRED")).toMatch(/^Not currently authorized/);
    expect(pickupStatusLabel("AUTHORIZATION_NOT_STARTED")).toMatch(/^Not currently authorized/);
    expect(pickupStatusLabel("ANYTHING_ELSE")).toBe("Not currently authorized");
    expect(guardianErrorMessage("authorization_expired")).toContain("ended");
    expect(guardianErrorMessage("unknown_category")).toBe("The change could not be saved. Try again.");
  });
});

describe("release workflow (V1-04E)", () => {
  it("offers three operator-statement verification methods and nothing biometric", () => {
    expect(VERIFICATION_METHODS.map((item) => item.method)).toEqual(["KNOWN_TO_STAFF", "OPERATOR_CONFIRMED", "PHOTO_ID_CHECKED"]);
    const photo = VERIFICATION_METHODS.find((item) => item.method === "PHOTO_ID_CHECKED");
    expect(photo?.help).toContain("Nothing is scanned, copied or stored");
  });

  it("nothing is selected by default and every step is required", () => {
    expect(EMPTY_RELEASE_SELECTION).toEqual({ guardianContactId: null, method: null, confirmed: false });
    expect(releaseReadiness(CHILD, CANDIDATES, EMPTY_RELEASE_SELECTION)).toEqual({
      ready: false,
      missing: ["person", "method", "confirmation"],
    });
    const person = { ...EMPTY_RELEASE_SELECTION, guardianContactId: ADULT };
    expect(releaseReadiness(CHILD, CANDIDATES, person)).toEqual({ ready: false, missing: ["method", "confirmation"] });
    const method = { ...person, method: "OPERATOR_CONFIRMED" };
    expect(releaseReadiness(CHILD, CANDIDATES, method)).toEqual({ ready: false, missing: ["confirmation"] });
    expect(releaseReadiness(CHILD, CANDIDATES, { ...method, confirmed: true })).toEqual({
      ready: true,
      payload: { child_profile_id: CHILD, guardian_contact_id: ADULT, verification_method: "OPERATOR_CONFIRMED" },
    });
  });

  it("an adult who is not currently authorized can never be submitted", () => {
    const outsider = { guardianContactId: OTHER, method: "KNOWN_TO_STAFF", confirmed: true };
    expect(releaseReadiness(CHILD, CANDIDATES, outsider)).toEqual({ ready: false, missing: ["person"] });
    expect(releaseReadiness(CHILD, [], { ...outsider, guardianContactId: ADULT })).toEqual({ ready: false, missing: ["person"] });
    const bogus = { guardianContactId: ADULT, method: "FACE_MATCH", confirmed: true };
    expect(releaseReadiness(CHILD, CANDIDATES, bogus)).toEqual({ ready: false, missing: ["method"] });
  });

  it("the BFF release body is exactly a child, an adult and a bounded method", () => {
    const body = { child_profile_id: CHILD, guardian_contact_id: ADULT, verification_method: "PHOTO_ID_CHECKED" };
    expect(parseRelease(body)).toEqual(body);
    expect(parseRelease({ child_profile_id: CHILD, guardian_contact_id: ADULT })).toBeNull();
    for (const method of ["", "FACE_MATCH", "OTHER_MANUAL", "operator_confirmed"]) {
      expect(parseRelease({ ...body, verification_method: method })).toBeNull();
    }
    for (const extra of [{ track_id: 1 }, { camera_id: OTHER }, { note: "x" }, { id_number: "D123" }, { image: "x" }]) {
      expect(parseRelease({ ...body, ...extra })).toBeNull();
    }
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

describe("guardian UI source guarantees (V1-04E)", () => {
  const release = join(__dirname, "app/app/classrooms/[classroomId]/child-release.tsx");
  const files = [
    join(__dirname, "lib/guardians.ts"),
    ...sources(join(__dirname, "app/app/guardians")),
    ...sources(join(__dirname, "app/app/children/[childId]")),
    ...sources(join(__dirname, "app/api/guardians")),
    ...sources(join(__dirname, "app/api/children/[childId]/guardians")),
    ...sources(join(__dirname, "app/api/facilities/[facilityId]/guardians")),
    ...sources(join(__dirname, "app/api/classrooms/[classroomId]/attendance/release")),
    release,
  ];

  it("has no photo upload, camera, image or biometric UI", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const forbidden of ["<img", "<video", 'type="file"', "FormData", "getUserMedia", "enrollment-images", "recognition-test"]) {
        expect(text, file).not.toContain(forbidden);
      }
      expect(text, file).not.toMatch(/face_|faceMatch|photo_url|embedding|track_id|kinship|resemblance/i);
    }
  });

  it("uses no biometric wording and claims no legal verification", () => {
    for (const file of files) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const claim of [
        "biometric",
        "facial",
        "face recognition",
        "fingerprint",
        "legally verified",
        "legal verification",
        "verified identity",
        "identity verified",
        "legal guardian verified",
        "compliant",
        "licensed",
        "guarantee",
      ]) {
        expect(text, file).not.toContain(claim);
      }
    }
  });

  it("the release form never pre-selects a person or a method and never auto-submits", () => {
    const text = readFileSync(release, "utf8");
    expect(text).not.toContain("defaultChecked");
    expect(text).not.toMatch(/candidates\[0\]|autoFocus|requestSubmit|\.submit\(\)/);
    expect(text).toContain("useState<ReleaseSelection>(EMPTY_RELEASE_SELECTION)");
    expect(text).toContain("disabled={working || !readiness.ready}");
    expect(text).toContain("options.candidates.map");
    expect(text).not.toContain("options.unavailable.map((item) => (\n            <label");
    expect(text).toContain("VeoTrex does not identify anyone");
  });

  it("the classroom card separates a release from an administrative check-out", () => {
    const card = readFileSync(join(__dirname, "app/app/classrooms/[classroomId]/child-attendance-card.tsx"), "utf8");
    expect(card).toContain("Administrative check-out");
    expect(card).toContain("<ChildRelease");
    expect(card).toContain('"Released to an authorized adult"');
  });
});
