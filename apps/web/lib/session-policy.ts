export const LOGIN_PATH = "/auth/login";
export const LOGOUT_PATH = "/auth/logout";
export const APP_PATH = "/app";
// Auth0 owns registration: VeoTrex never renders a credential form. `screen_hint`
// opens Universal Login on its sign-up view, and `prompt=login` forces a fresh
// transaction so a lingering session cannot silently skip the screen.
export const SIGNUP_PATH = `${LOGIN_PATH}?screen_hint=signup&prompt=login`;

export type SessionLike = { user?: { name?: unknown } } | null | undefined;

export type SafeSessionView = Readonly<{
  authenticated: boolean;
  displayName: string | null;
}>;

export function safeSessionView(session: SessionLike): SafeSessionView {
  const name = session?.user?.name;
  return {
    authenticated: Boolean(session?.user),
    displayName: typeof name === "string" && name.trim() ? name : null,
  };
}

export function protectedRouteRedirect(session: SessionLike): string | null {
  return session?.user ? null : LOGIN_PATH;
}

export type LandingAction = Readonly<{
  label: string;
  href: string;
  emphasis: "primary" | "secondary" | "plain";
}>;

/**
 * The landing page's action set. Sign up is offered only to visitors without a session:
 * an authenticated user has an identity already, and showing it again invites a confusing
 * second registration. Authentication is not authorization - neither action grants a tenant.
 */
export function landingActions(session: SessionLike): readonly LandingAction[] {
  if (session?.user) {
    return [
      { label: "Open VeoTrex", href: APP_PATH, emphasis: "primary" },
      { label: "Log out", href: LOGOUT_PATH, emphasis: "plain" },
    ];
  }
  return [
    { label: "Log in", href: LOGIN_PATH, emphasis: "primary" },
    { label: "Sign up", href: SIGNUP_PATH, emphasis: "secondary" },
  ];
}
