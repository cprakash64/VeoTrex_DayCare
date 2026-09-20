/**
 * Authorization parameters for the Auth0 client, and the login precondition.
 *
 * The API verifier requires an `org_id` claim on every access token
 * (apps/api/src/veotrex_api/identity.py), and Auth0 emits that claim only when the
 * authorization request names an organization. Starting a login without one therefore
 * produces a session whose every API call returns 401.
 *
 * That makes the organization a precondition for *authentication*, not for module
 * evaluation. Nothing here throws: `lib/auth0.ts` must remain importable, and the public
 * landing page and `next build` must work on a deployment that has not configured an
 * organization yet. The guard lives at the login boundary instead - see `lib/auth-guard.ts`.
 *
 * The organization identifier is environment-specific and never committed.
 */
export type Auth0Env = Readonly<Record<string, string | undefined>>;

export const ORGANIZATION_ENV = "AUTH0_ORGANIZATION_ID";
export const AUDIENCE_ENV = "AUTH0_AUDIENCE";
export const AUTH_SCOPE = "openid profile email";

export type VeotrexAuthorizationParameters = {
  audience: string | undefined;
  scope: string;
  organization?: string;
};

/** The configured organization, or null when absent or blank. Never throws. */
export function organizationFrom(env: Auth0Env): string | null {
  const value = env[ORGANIZATION_ENV]?.trim();
  return value ? value : null;
}

export function isOrganizationConfigured(env: Auth0Env): boolean {
  return organizationFrom(env) !== null;
}

/**
 * Omits `organization` entirely when none is configured rather than sending an empty
 * value, which Auth0 would reject as a malformed authorization request.
 */
export function authorizationParametersFrom(env: Auth0Env): VeotrexAuthorizationParameters {
  const organization = organizationFrom(env);
  const parameters: VeotrexAuthorizationParameters = {
    audience: env[AUDIENCE_ENV],
    scope: AUTH_SCOPE,
  };
  if (organization !== null) {
    parameters.organization = organization;
  }
  return parameters;
}
