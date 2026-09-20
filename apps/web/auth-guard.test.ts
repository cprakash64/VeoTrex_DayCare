import { describe, expect, it } from "vitest";

import {
  LOGIN_PATHNAME,
  MISCONFIGURED_MESSAGE,
  MISCONFIGURED_STATUS,
  loginIsBlocked,
} from "./lib/auth-guard";
import { ORGANIZATION_ENV } from "./lib/auth0-config";

const CONFIGURED = { [ORGANIZATION_ENV]: "org_ci_example_not_real" };
const ABSENT = {};

describe("login organization guard", () => {
  it("blocks starting a login when no organization is configured", () => {
    expect(loginIsBlocked(LOGIN_PATHNAME, ABSENT)).toBe(true);
    for (const blank of ["", "   "]) {
      expect(loginIsBlocked(LOGIN_PATHNAME, { [ORGANIZATION_ENV]: blank })).toBe(true);
    }
  });

  it("allows login through to Auth0 once an organization is configured", () => {
    expect(loginIsBlocked(LOGIN_PATHNAME, CONFIGURED)).toBe(false);
  });

  it("never blocks callback or logout, configured or not", () => {
    for (const pathname of ["/auth/callback", "/auth/logout", "/auth/profile"]) {
      expect(loginIsBlocked(pathname, ABSENT)).toBe(false);
      expect(loginIsBlocked(pathname, CONFIGURED)).toBe(false);
    }
  });

  it("never blocks public or application routes", () => {
    for (const pathname of ["/", "/app", "/integrations/ring/link", "/app/x"]) {
      expect(loginIsBlocked(pathname, ABSENT)).toBe(false);
    }
  });

  it("answers a blocked login with a non-cacheable configuration error", () => {
    expect(MISCONFIGURED_STATUS).toBe(503);
    expect(MISCONFIGURED_MESSAGE).not.toMatch(/org_[A-Za-z0-9]{4,}/);
  });
});
