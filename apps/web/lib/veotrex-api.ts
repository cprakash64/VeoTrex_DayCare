import "server-only";

import { auth0 } from "./auth0";

export type ApplicationIdentity = Readonly<{
  actor_id: string;
  tenant_id: string;
  display_name: string | null;
  roles: ReadonlyArray<{ role: string; facility_id: string | null }>;
  permissions: ReadonlyArray<string>;
}>;

export async function getApplicationIdentity(): Promise<ApplicationIdentity> {
  const response = await authorizedFetch("/v1/me");
  if (!response.ok) {
    throw new Error("VeoTrex application access was denied");
  }
  return (await response.json()) as ApplicationIdentity;
}

function apiBaseUrl(): string {
  const baseUrl = process.env.VEOTREX_API_BASE_URL;
  if (!baseUrl) {
    throw new Error("VeoTrex API is not configured");
  }
  return baseUrl.replace(/\/$/, "");
}

async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const { token } = await auth0.getAccessToken();
  return fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers: {
      ...init?.headers,
      Authorization: `Bearer ${token}`,
    },
    cache: "no-store",
  });
}

export type RingLinkContext = Readonly<{ tenant_name: string; eligible: boolean }>;

export async function getRingLinkContext(
  nonce: string,
  time: string,
): Promise<RingLinkContext> {
  const query = new URLSearchParams({ nonce, time });
  const response = await authorizedFetch(`/v1/integrations/ring/link-context?${query}`);
  if (!response.ok) {
    throw new Error("Ring link context is unavailable");
  }
  return (await response.json()) as RingLinkContext;
}

export async function claimRingLink(nonce: string, time: number): Promise<Response> {
  return authorizedFetch("/v1/integrations/ring/claim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nonce, time }),
  });
}

export type RingConnection = Readonly<{
  connection_id: string;
  display_name: string;
  provider: "RING";
  status: string;
  integration_state: string;
  operational_health: string;
  last_synchronized_at: string | null;
  last_sync_failure_category: string | null;
}>;

export async function getRingConnections(): Promise<ReadonlyArray<RingConnection>> {
  const response = await authorizedFetch("/v1/integrations/ring/connections");
  if (!response.ok) throw new Error("Ring connections are unavailable");
  return (await response.json()) as ReadonlyArray<RingConnection>;
}

export type RingInventoryCamera = Readonly<{
  camera_id: string;
  connection_id: string;
  display_name: string;
  provider: "RING";
  inventory_state: string;
  provider_online: boolean | null;
  capabilities: ReadonlyArray<string>;
  assigned: boolean;
  privacy_controls_configured: boolean;
  last_synchronized_at: string | null;
}>;

export async function getRingInventory(): Promise<ReadonlyArray<RingInventoryCamera>> {
  const response = await authorizedFetch("/v1/integrations/ring/devices");
  if (!response.ok) throw new Error("Ring inventory is unavailable");
  return (await response.json()) as ReadonlyArray<RingInventoryCamera>;
}

export async function syncRingConnection(connectionId: string): Promise<Response> {
  return authorizedFetch(`/v1/integrations/ring/connections/${connectionId}/sync`, {
    method: "POST",
  });
}

// ------------------------------------------------------------------ staff enrollment (V1-02A)
import type { EnrollmentImageSummary, StaffSummary } from "./staff";

export async function getStaff(): Promise<ReadonlyArray<StaffSummary>> {
  const response = await authorizedFetch("/v1/staff");
  if (!response.ok) throw new Error("Staff roster is unavailable");
  return (await response.json()) as ReadonlyArray<StaffSummary>;
}

export async function getStaffMember(staffId: string): Promise<StaffSummary | null> {
  const response = await authorizedFetch(`/v1/staff/${encodeURIComponent(staffId)}`);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Staff profile is unavailable");
  return (await response.json()) as StaffSummary;
}

export async function getStaffImages(staffId: string): Promise<ReadonlyArray<EnrollmentImageSummary>> {
  const response = await authorizedFetch(`/v1/staff/${encodeURIComponent(staffId)}/enrollment-images`);
  if (!response.ok) throw new Error("Enrollment photos are unavailable");
  return (await response.json()) as ReadonlyArray<EnrollmentImageSummary>;
}

export async function createStaff(displayName: string): Promise<Response> {
  return authorizedFetch("/v1/staff", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function renameStaff(staffId: string, displayName: string): Promise<Response> {
  return authorizedFetch(`/v1/staff/${encodeURIComponent(staffId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function setStaffActive(staffId: string, active: boolean): Promise<Response> {
  return authorizedFetch(
    `/v1/staff/${encodeURIComponent(staffId)}/${active ? "activate" : "deactivate"}`,
    { method: "POST" },
  );
}

export async function deleteStaff(staffId: string): Promise<Response> {
  return authorizedFetch(`/v1/staff/${encodeURIComponent(staffId)}`, { method: "DELETE" });
}

export async function uploadStaffImage(
  staffId: string,
  bytes: ArrayBuffer,
  mediaType: string,
): Promise<Response> {
  return authorizedFetch(`/v1/staff/${encodeURIComponent(staffId)}/enrollment-images`, {
    method: "POST",
    headers: { "Content-Type": mediaType },
    body: bytes,
  });
}

export async function deleteStaffImage(staffId: string, imageId: string): Promise<Response> {
  return authorizedFetch(
    `/v1/staff/${encodeURIComponent(staffId)}/enrollment-images/${encodeURIComponent(imageId)}`,
    { method: "DELETE" },
  );
}

export async function getStaffImageContent(staffId: string, imageId: string): Promise<Response> {
  return authorizedFetch(
    `/v1/staff/${encodeURIComponent(staffId)}/enrollment-images/${encodeURIComponent(imageId)}/content`,
  );
}

/**
 * Local recognition evaluation (V1-02B0). The route exists on the API only in an evaluation
 * environment; elsewhere this call reaches a path that is not registered and fails, which is
 * the intended outcome rather than something to handle specially.
 */
export async function runRecognitionTest(bytes: ArrayBuffer, mediaType: string): Promise<Response> {
  return authorizedFetch("/v1/staff/recognition-test", {
    method: "POST",
    headers: { "Content-Type": mediaType },
    body: bytes,
  });
}

// ------------------------------------------------ classrooms and configured ratio policy (V1-04A)
import type {
  Classroom,
  ClassroomPayload,
  FacilitySummary,
  PolicyPayload,
  RatioStatus,
} from "./classrooms";

export async function getFacilities(): Promise<ReadonlyArray<FacilitySummary>> {
  const response = await authorizedFetch("/v1/facilities");
  if (!response.ok) throw new Error("Facilities are unavailable");
  return (await response.json()) as ReadonlyArray<FacilitySummary>;
}

export async function getClassrooms(): Promise<ReadonlyArray<Classroom>> {
  const response = await authorizedFetch("/v1/classrooms");
  if (!response.ok) throw new Error("Classrooms are unavailable");
  return (await response.json()) as ReadonlyArray<Classroom>;
}

export async function getClassroom(classroomId: string): Promise<Classroom | null> {
  const response = await authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}`);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Classroom is unavailable");
  return (await response.json()) as Classroom;
}

export async function getClassroomRatioStatus(classroomId: string): Promise<RatioStatus | null> {
  const response = await authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/ratio-status`,
  );
  if (!response.ok) return null;
  return (await response.json()) as RatioStatus;
}

export async function createClassroom(
  facilityId: string,
  payload: ClassroomPayload,
): Promise<Response> {
  return authorizedFetch("/v1/classrooms", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ facility_id: facilityId, ...payload }),
  });
}

export async function updateClassroom(
  classroomId: string,
  payload: ClassroomPayload,
): Promise<Response> {
  return authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function setClassroomActive(classroomId: string, active: boolean): Promise<Response> {
  return authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/${active ? "activate" : "deactivate"}`,
    { method: "POST" },
  );
}

export async function createRatioPolicy(
  classroomId: string,
  payload: PolicyPayload,
): Promise<Response> {
  return authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}/ratio-policies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateRatioPolicy(
  classroomId: string,
  policyId: string,
  payload: PolicyPayload,
): Promise<Response> {
  return authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/ratio-policies/${encodeURIComponent(policyId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function deactivateRatioPolicy(classroomId: string, policyId: string): Promise<Response> {
  return authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/ratio-policies/${encodeURIComponent(policyId)}/deactivate`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------- manual presence (V1-04B)
import type { ClassroomPresence, ManualPresencePayload } from "./classrooms";

export async function getClassroomPresence(classroomId: string): Promise<ClassroomPresence | null> {
  const response = await authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}/presence`);
  if (!response.ok) return null;
  return (await response.json()) as ClassroomPresence;
}

export async function submitManualPresence(
  classroomId: string,
  payload: ManualPresencePayload,
): Promise<Response> {
  return authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}/presence/manual`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function revokeManualPresence(classroomId: string, snapshotId: string): Promise<Response> {
  return authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/presence/${encodeURIComponent(snapshotId)}/revoke`,
    { method: "POST" },
  );
}

// -------------------------------------------------- staff roster and staff check-in (V1-04C)
import type {
  ClassroomStaffPresence,
  EligibilityCreatePayload,
  EligibilityUpdatePayload,
  FacilityRoster,
  PresenceMode,
  StaffPresencePayload,
} from "./staff-presence";

export async function getClassroomStaffPresence(classroomId: string): Promise<ClassroomStaffPresence | null> {
  const response = await authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/staff-presence`,
  );
  if (!response.ok) return null;
  return (await response.json()) as ClassroomStaffPresence;
}

export async function recordStaffPresence(
  classroomId: string,
  action: "check-in" | "check-out" | "refresh",
  payload: StaffPresencePayload,
): Promise<Response> {
  return authorizedFetch(
    `/v1/classrooms/${encodeURIComponent(classroomId)}/staff-presence/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function setPresenceSourceMode(classroomId: string, mode: PresenceMode): Promise<Response> {
  return authorizedFetch(`/v1/classrooms/${encodeURIComponent(classroomId)}/presence-source-mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

export async function getFacilityRoster(
  facilityId: string,
  staffProfileId?: string,
): Promise<FacilityRoster | null> {
  const query = staffProfileId ? `?${new URLSearchParams({ staff_profile_id: staffProfileId })}` : "";
  const response = await authorizedFetch(
    `/v1/facilities/${encodeURIComponent(facilityId)}/staff-ratio-eligibility${query}`,
  );
  if (!response.ok) return null;
  return (await response.json()) as FacilityRoster;
}

export async function createEligibility(
  facilityId: string,
  payload: EligibilityCreatePayload,
): Promise<Response> {
  return authorizedFetch(`/v1/facilities/${encodeURIComponent(facilityId)}/staff-ratio-eligibility`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateEligibility(
  facilityId: string,
  eligibilityId: string,
  payload: EligibilityUpdatePayload,
): Promise<Response> {
  return authorizedFetch(
    `/v1/facilities/${encodeURIComponent(facilityId)}/staff-ratio-eligibility/${encodeURIComponent(eligibilityId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export async function deactivateEligibility(facilityId: string, eligibilityId: string): Promise<Response> {
  return authorizedFetch(
    `/v1/facilities/${encodeURIComponent(facilityId)}/staff-ratio-eligibility/${encodeURIComponent(eligibilityId)}/deactivate`,
    { method: "POST" },
  );
}
