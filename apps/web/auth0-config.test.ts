import { describe, expect, it } from "vitest";

import {
  AUDIENCE_ENV,
  AUTH_SCOPE,
  ORGANIZATION_ENV,
  authorizationParametersFrom,
  isOrganizationConfigured,
  organizationFrom,
} from "./lib/auth0-config";

const AUDIENCE = "https://daycare.veotrex.com/api";
const ORGANIZATION = "org_ci_example_not_real";

describe("Auth0 authorization parameters", () => {
  it("requests the configured organization so Auth0 emits org_id", () => {
    const params = authorizationParametersFrom({
      [ORGANIZATION_ENV]: ORGANIZATION,
      [AUDIENCE_ENV]: AUDIENCE,
    });
    expect(params.organization).toBe(ORGANIZATION);
  });

  it("leaves audience and scope exactly as they were", () => {
    for (const env of [
      { [ORGANIZATION_ENV]: ORGANIZATION, [AUDIENCE_ENV]: AUDIENCE },
      { [AUDIENCE_ENV]: AUDIENCE },
    ]) {
      const params = authorizationParametersFrom(env);
      expect(params.audience).toBe(AUDIENCE);
      expect(params.scope).toBe("openid profile email");
    }
    expect(AUTH_SCOPE).toBe("openid profile email");
  });

  it("takes the organization from the environment, never from source", () => {
    expect(
      authorizationParametersFrom({ [ORGANIZATION_ENV]: "org_first" }).organization,
    ).toBe("org_first");
    expect(
      authorizationParametersFrom({ [ORGANIZATION_ENV]: "org_second" }).organization,
    ).toBe("org_second");
  });

  it("names no organization identifier in its own source", () => {
    const source = `${authorizationParametersFrom.toString()}${organizationFrom.toString()}`;
    expect(source).not.toMatch(/org_[A-Za-z0-9]{4,}/);
  });

  it("omits organization entirely when absent or blank, and never throws", () => {
    for (const value of [undefined, "", "   "]) {
      const env = { [ORGANIZATION_ENV]: value, [AUDIENCE_ENV]: AUDIENCE };
      expect(() => authorizationParametersFrom(env)).not.toThrow();
      const params = authorizationParametersFrom(env);
      expect("organization" in params).toBe(false);
      expect(organizationFrom(env)).toBeNull();
      expect(isOrganizationConfigured(env)).toBe(false);
    }
  });

  it("builds usable parameters from a completely empty environment", () => {
    // This is the case that must not throw: lib/auth0.ts is evaluated at module scope by
    // `next build` while prerendering the public landing page.
    const params = authorizationParametersFrom({});
    expect(params.scope).toBe("openid profile email");
    expect(params.audience).toBeUndefined();
    expect("organization" in params).toBe(false);
  });
});
