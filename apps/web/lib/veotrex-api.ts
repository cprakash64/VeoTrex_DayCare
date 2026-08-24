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
