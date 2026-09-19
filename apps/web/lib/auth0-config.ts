/**
 * Authorization parameters for the Auth0 client.
 *
 * The API verifier requires an `org_id` claim on every access token
 * (apps/api/src/veotrex_api/identity.py), and Auth0 emits that claim only when the
 * authorization request names an organization. Requesting one is therefore not optional
 * for this deployment: without it every API call returns 401, which is why a missing
 * organization fails closed here rather than degrading silently at request time.
 *
 * The organization identifier is environment-specific and never committed.
 */
export type Auth0Env = Readonly<Record<string, string | undefined>>;

export const ORGANIZATION_ENV = "AUTH0_ORGANIZATION_ID";
export const AUDIENCE_ENV = "AUTH0_AUDIENCE";
export const AUTH_SCOPE = "openid profile email";

export type VeotrexAuthorizationParameters = Readonly<{
  audience: string | undefined;
  scope: string;
  organization: string;
}>;

export function authorizationParametersFrom(env: Auth0Env): VeotrexAuthorizationParameters {
  const organization = env[ORGANIZATION_ENV]?.trim();
  if (!organization) {
    throw new Error(
      `${ORGANIZATION_ENV} is not configured. VeoTrex requires an Auth0 Organization ` +
        "because the API rejects any access token without an org_id claim.",
    );
  }
  return {
    audience: env[AUDIENCE_ENV],
    scope: AUTH_SCOPE,
    organization,
  };
}
