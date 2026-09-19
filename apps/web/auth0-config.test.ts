import { describe, expect, it } from "vitest";

import {
  AUDIENCE_ENV,
  AUTH_SCOPE,
  ORGANIZATION_ENV,
  authorizationParametersFrom,
} from "./lib/auth0-config";

const AUDIENCE = "https://daycare.veotrex.com/api";
const ORGANIZATION = "org_example_not_a_real_identifier";

describe("Auth0 authorization parameters", () => {
  it("requests the configured organization so Auth0 emits org_id", () => {
    const params = authorizationParametersFrom({
      [ORGANIZATION_ENV]: ORGANIZATION,
      [AUDIENCE_ENV]: AUDIENCE,
    });
    expect(params.organization).toBe(ORGANIZATION);
  });

  it("leaves audience and scope exactly as they were", () => {
    const params = authorizationParametersFrom({
      [ORGANIZATION_ENV]: ORGANIZATION,
      [AUDIENCE_ENV]: AUDIENCE,
    });
    expect(params.audience).toBe(AUDIENCE);
    expect(params.scope).toBe("openid profile email");
    expect(AUTH_SCOPE).toBe("openid profile email");
  });

  it("takes the organization from the environment, never from source", () => {
    const first = authorizationParametersFrom({
      [ORGANIZATION_ENV]: "org_first",
      [AUDIENCE_ENV]: AUDIENCE,
    });
    const second = authorizationParametersFrom({
      [ORGANIZATION_ENV]: "org_second",
      [AUDIENCE_ENV]: AUDIENCE,
    });
    expect(first.organization).toBe("org_first");
    expect(second.organization).toBe("org_second");
  });

  it("fails closed when the organization is absent or blank", () => {
    for (const value of [undefined, "", "   "]) {
      expect(() =>
        authorizationParametersFrom({
          [ORGANIZATION_ENV]: value,
          [AUDIENCE_ENV]: AUDIENCE,
        }),
      ).toThrow(/AUTH0_ORGANIZATION_ID/);
    }
  });

  it("names no organization identifier in its own source", () => {
    const source = authorizationParametersFrom.toString();
    expect(source).not.toMatch(/org_[A-Za-z0-9]{6,}/);
  });
});
