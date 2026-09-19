import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  APP_PATH,
  LOGIN_PATH,
  LOGOUT_PATH,
  SIGNUP_PATH,
  landingActions,
} from "./lib/session-policy";

const labels = (session: Parameters<typeof landingActions>[0]) =>
  landingActions(session).map((action) => action.label);

const hrefOf = (session: Parameters<typeof landingActions>[0], label: string) =>
  landingActions(session).find((action) => action.label === label)?.href;

describe("landing page actions", () => {
  it("offers a visitor both log in and sign up", () => {
    expect(labels(null)).toEqual(["Log in", "Sign up"]);
    expect(hrefOf(null, "Log in")).toBe(LOGIN_PATH);
  });

  it("sends sign up to Auth0 universal login with a sign-up hint", () => {
    const signup = hrefOf(null, "Sign up");
    expect(signup).toBe(SIGNUP_PATH);
    expect(signup).toContain("screen_hint=signup");
    expect(signup).toContain("prompt=login");
    expect(signup?.startsWith(LOGIN_PATH)).toBe(true);
  });

  it("makes log in primary and sign up secondary but present", () => {
    const actions = landingActions(null);
    expect(actions[0]).toMatchObject({ label: "Log in", emphasis: "primary" });
    expect(actions[1]).toMatchObject({ label: "Sign up", emphasis: "secondary" });
  });

  it("never offers sign up to an authenticated user", () => {
    const session = { user: { name: "Safety Lead" } };
    expect(labels(session)).toEqual(["Open VeoTrex", "Log out"]);
    expect(labels(session)).not.toContain("Sign up");
    expect(hrefOf(session, "Open VeoTrex")).toBe(APP_PATH);
    expect(hrefOf(session, "Log out")).toBe(LOGOUT_PATH);
  });

  it("leaves logout presentation untouched", () => {
    const session = { user: { name: "Safety Lead" } };
    const logout = landingActions(session).find((a) => a.label === "Log out");
    expect(logout?.emphasis).toBe("plain");
  });

  it("uses only SDK-mounted auth routes, never the legacy v3 shape", () => {
    for (const href of [LOGIN_PATH, LOGOUT_PATH, SIGNUP_PATH]) {
      expect(href.startsWith("/auth/")).toBe(true);
      expect(href).not.toContain("/api/auth");
    }
  });

  it("introduces no legacy /api/auth route handler", () => {
    const webRoot = path.dirname(fileURLToPath(import.meta.url));
    expect(existsSync(path.join(webRoot, "app", "api", "auth"))).toBe(false);
  });
});
