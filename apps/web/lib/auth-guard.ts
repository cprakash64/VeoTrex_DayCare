/**
 * The login precondition, kept as a pure function so it can be tested without Next's
 * request machinery and reused by `proxy.ts`.
 *
 * Only the login initiation path is guarded. `/auth/callback` must stay reachable so an
 * in-flight transaction can complete, and `/auth/logout` must stay reachable so a user is
 * never trapped in a session they cannot end.
 */
import { type Auth0Env, isOrganizationConfigured } from "./auth0-config";

export const LOGIN_PATHNAME = "/auth/login";

/** 503: the server is correctly configured in code but missing deployment configuration. */
export const MISCONFIGURED_STATUS = 503;
export const MISCONFIGURED_MESSAGE =
  "Sign-in is unavailable: this deployment has no Auth0 organization configured.";

export function loginIsBlocked(pathname: string, env: Auth0Env): boolean {
  return pathname === LOGIN_PATHNAME && !isOrganizationConfigured(env);
}
