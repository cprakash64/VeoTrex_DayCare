import { describe, expect, it } from "vitest";

import {
  LOGIN_PATH,
  LOGOUT_PATH,
  protectedRouteRedirect,
  safeSessionView,
} from "./lib/session-policy";

describe("frontend authentication boundary", () => {
  it("redirects an unauthenticated protected route to login", () => {
    expect(protectedRouteRedirect(null)).toBe(LOGIN_PATH);
    expect(safeSessionView(null)).toEqual({ authenticated: false, displayName: null });
  });

  it("allows an authenticated session and renders only safe display data", () => {
    const session = {
      user: { name: "Safety Lead" },
      accessToken: "must-never-render",
      idToken: "must-never-render-either",
    };
    const view = safeSessionView(session);
    expect(protectedRouteRedirect(session)).toBeNull();
    expect(view).toEqual({ authenticated: true, displayName: "Safety Lead" });
    expect(JSON.stringify(view)).not.toContain("must-never-render");
  });

  it("uses the SDK-mounted login and logout controls", () => {
    expect(LOGIN_PATH).toBe("/auth/login");
    expect(LOGOUT_PATH).toBe("/auth/logout");
  });
});
