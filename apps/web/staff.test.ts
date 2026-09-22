import { describe, expect, it } from "vitest";

import {
  clientUploadCheck,
  MAX_UPLOAD_BYTES,
  rejectionMessage,
  staffPresentation,
  type StaffSummary,
} from "./lib/staff";

function staff(overrides: Partial<StaffSummary> = {}): StaffSummary {
  return {
    staff_id: "11111111-1111-4111-8111-111111111111",
    display_name: "Chandra Pandey",
    status: "ACTIVE",
    enrollment_state: "EMPTY",
    accepted_images: 0,
    required_images: 3,
    maximum_images: 5,
    recognition_ready: false,
    created_at: "2026-09-22T00:00:00Z",
    updated_at: "2026-09-22T00:00:00Z",
    ...overrides,
  };
}

describe("staff presentation (V1-02A)", () => {
  it("empty profile: no photos, upload allowed, not ready", () => {
    const view = staffPresentation(staff());
    expect(view.readinessLabel).toBe("No photos yet");
    expect(view.progressLabel).toBe("0/5 photos");
    expect(view.canUpload).toBe(true);
    expect(view.statusLabel).toBe("Active");
  });

  it("collecting: readiness is the backend's word, not the count", () => {
    const view = staffPresentation(staff({ enrollment_state: "COLLECTING", accepted_images: 2 }));
    expect(view.readinessLabel).toBe("Collecting photos · 1 more needed");
    expect(view.progressLabel).toBe("2/5 photos");
    // Three photos but the backend has not said READY: still not ready.
    const notYet = staffPresentation(staff({ enrollment_state: "COLLECTING", accepted_images: 3 }));
    expect(notYet.readinessLabel).toContain("Collecting");
  });

  it("ready, full, inactive and failed states", () => {
    expect(
      staffPresentation(staff({ enrollment_state: "READY", accepted_images: 3, recognition_ready: true }))
        .readinessLabel,
    ).toBe("Recognition ready");
    const full = staffPresentation(staff({ enrollment_state: "READY", accepted_images: 5, recognition_ready: true }));
    expect(full.canUpload).toBe(false);
    const inactive = staffPresentation(
      staff({ status: "INACTIVE", enrollment_state: "READY", accepted_images: 3, recognition_ready: false }),
    );
    expect(inactive.statusLabel).toBe("Inactive");
    expect(inactive.readinessLabel).toBe("Inactive · not recognized");
    expect(inactive.canUpload).toBe(false);
    expect(inactive.canActivate).toBe(true);
    expect(inactive.canDeactivate).toBe(false);
    expect(staffPresentation(staff({ enrollment_state: "FAILED", accepted_images: 3 })).readinessLabel).toContain(
      "Needs attention",
    );
    expect(staffPresentation(staff({ enrollment_state: "PROCESSING", accepted_images: 1 })).readinessLabel).toBe(
      "Processing photos",
    );
  });

  it("rejection categories map to bounded human messages", () => {
    expect(rejectionMessage("multiple_faces")).toContain("More than one person");
    expect(rejectionMessage("no_face_detected")).toContain("No face");
    expect(rejectionMessage("face_backend_unavailable")).toContain("not available");
    expect(rejectionMessage("something_else")).toBe("The photo was not accepted.");
    expect(rejectionMessage(undefined)).toBe("The photo was not accepted.");
  });

  it("client-side pre-check mirrors the server's type and size limits", () => {
    expect(clientUploadCheck({ size: 1000, type: "image/jpeg" })).toBeNull();
    expect(clientUploadCheck({ size: 1000, type: "image/png" })).toBeNull();
    expect(clientUploadCheck({ size: 1000, type: "image/gif" })).toContain("JPEG and PNG");
    expect(clientUploadCheck({ size: 0, type: "image/jpeg" })).toContain("empty");
    expect(clientUploadCheck({ size: MAX_UPLOAD_BYTES + 1, type: "image/jpeg" })).toContain("8 MB");
  });
});
