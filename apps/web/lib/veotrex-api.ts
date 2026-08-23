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
  const { token } = await auth0.getAccessToken();
  const baseUrl = process.env.VEOTREX_API_BASE_URL;
  if (!baseUrl) {
    throw new Error("VeoTrex API is not configured");
  }
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/v1/me`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("VeoTrex application access was denied");
  }
  return (await response.json()) as ApplicationIdentity;
}
