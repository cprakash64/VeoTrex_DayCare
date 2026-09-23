/**
 * Staff (teacher) enrollment presentation (V1-02A), extended with the local recognition
 * evaluation panel (V1-02B0). Pure functions over the API's own summaries: the backend decides
 * readiness and identity, the UI only words them. Nothing here touches a template, a media key
 * or a filesystem path, and no score is ever presented as a production-calibrated confidence.
 */

export type StaffSummary = Readonly<{
  staff_id: string;
  display_name: string;
  status: string;
  enrollment_state: string;
  accepted_images: number;
  required_images: number;
  maximum_images: number;
  recognition_ready: boolean;
  created_at: string;
  updated_at: string;
}>;

export type EnrollmentImageSummary = Readonly<{
  image_id: string;
  width: number;
  height: number;
  byte_size: number;
  face_size_px: number | null;
  quality: number | null;
  template_state: string;
  created_at: string;
}>;

export const ACCEPTED_UPLOAD_TYPES = ["image/jpeg", "image/png"] as const;
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

export type StaffPresentation = Readonly<{
  statusLabel: string;
  progressLabel: string;
  readinessLabel: string;
  canUpload: boolean;
  canActivate: boolean;
  canDeactivate: boolean;
}>;

export function staffPresentation(staff: StaffSummary): StaffPresentation {
  const inactive = staff.status === "INACTIVE";
  const full = staff.accepted_images >= staff.maximum_images;
  let readinessLabel: string;
  if (staff.recognition_ready) readinessLabel = "Recognition ready";
  else if (inactive) readinessLabel = "Inactive · not recognized";
  else if (staff.enrollment_state === "FAILED") readinessLabel = "Needs attention · photo processing failed";
  else if (staff.enrollment_state === "PROCESSING") readinessLabel = "Processing photos";
  else if (staff.enrollment_state === "EMPTY") readinessLabel = "No photos yet";
  else readinessLabel = `Collecting photos · ${Math.max(staff.required_images - staff.accepted_images, 0)} more needed`;
  return {
    statusLabel: inactive ? "Inactive" : "Active",
    progressLabel: `${staff.accepted_images}/${staff.maximum_images} photos`,
    readinessLabel,
    canUpload: !inactive && !full,
    canActivate: inactive,
    canDeactivate: !inactive,
  };
}

const REJECTION_MESSAGES: Readonly<Record<string, string>> = {
  empty_upload: "The file was empty.",
  file_too_large: "The photo is larger than 8 MB.",
  unsupported_type: "Only JPEG and PNG photos are accepted.",
  invalid_image: "The file could not be read as a photo.",
  image_too_large: "The photo's dimensions are too large.",
  image_too_small: "The photo is too small; use at least 160×160 pixels.",
  no_face_detected: "No face was found. Use a clear, front-facing photo of the teacher alone.",
  multiple_faces: "More than one person is visible. Enrollment photos must show the teacher alone.",
  face_too_small: "The face is too small in the frame. Move closer or crop the photo.",
  face_too_blurry: "The photo is too blurry to use. Take a sharper one.",
  duplicate_image: "This photo was already enrolled.",
  enrollment_limit_reached: "This teacher already has the maximum number of photos.",
  face_backend_unavailable: "Face processing is not available in this deployment yet.",
  template_failed: "The photo could not be processed. Try a different photo.",
  profile_not_active: "Activate the teacher before adding photos.",
};

export function rejectionMessage(category: unknown): string {
  return (typeof category === "string" && REJECTION_MESSAGES[category]) || "The photo was not accepted.";
}

export function clientUploadCheck(file: { size: number; type: string }): string | null {
  if (!(ACCEPTED_UPLOAD_TYPES as readonly string[]).includes(file.type)) return REJECTION_MESSAGES.unsupported_type;
  if (file.size === 0) return REJECTION_MESSAGES.empty_upload;
  if (file.size > MAX_UPLOAD_BYTES) return REJECTION_MESSAGES.file_too_large;
  return null;
}

// ------------------------------------------------ local recognition evaluation (V1-02B0)

/**
 * Whether the evaluation panel may be rendered at all.
 *
 * The real boundary is the API, where the recognition route is not registered outside
 * local/development/test/ci. This is the second, independent gate: the dashboard must not
 * offer a control that only exists in an evaluation build, and the BFF route checks the same
 * flag so a hidden panel cannot be driven by a hand-made request either. Absent or anything
 * other than "1" means off, so a deployment that never sets it is correct by default.
 */
export const FACE_EVALUATION_ENV = "VEOTREX_FACE_EVALUATION_UI";

export function faceEvaluationEnabled(env: Readonly<Record<string, string | undefined>>): boolean {
  return env[FACE_EVALUATION_ENV]?.trim() === "1";
}

export type RecognitionTestResult = Readonly<{
  decision: "MATCH" | "UNKNOWN";
  staff_id: string | null;
  display_name: string | null;
  score: number;
  runner_up_score: number | null;
  reason: string | null;
  candidates: number;
  model_id: string;
  model_version: string;
  threshold: number;
  margin: number;
  evaluation_only: boolean;
}>;

export type RecognitionPresentation = Readonly<{
  headline: string;
  detail: string;
  matched: boolean;
}>;

const UNKNOWN_REASONS: Readonly<Record<string, string>> = {
  no_candidates: "No teacher in this organization is enrolled and ready to be recognized.",
  below_threshold:
    "The closest enrolled teacher was not close enough to name with confidence.",
  ambiguous:
    "This photo resembled more than one enrolled teacher about equally, so no name was chosen.",
};

/**
 * Words a decision for an operator. UNKNOWN never names the nearest candidate, not even as a
 * hint: the whole point of the threshold and the margin is that a refused identity stays
 * refused, and showing "probably Chandra" would hand back exactly what was withheld.
 */
export function recognitionPresentation(result: RecognitionTestResult): RecognitionPresentation {
  const score = `similarity ${result.score.toFixed(3)} (evaluation threshold ${result.threshold.toFixed(2)})`;
  if (result.decision === "MATCH" && result.display_name) {
    return {
      headline: `Match: ${result.display_name}`,
      detail: `${score}, compared against ${result.candidates} enrolled ${
        result.candidates === 1 ? "teacher" : "teachers"
      }.`,
      matched: true,
    };
  }
  const reason =
    (result.reason && UNKNOWN_REASONS[result.reason]) ||
    "This photo was not matched to an enrolled teacher.";
  return { headline: "Unknown", detail: `${reason} ${score}.`, matched: false };
}
