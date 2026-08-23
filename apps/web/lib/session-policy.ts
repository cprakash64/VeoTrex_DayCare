export const LOGIN_PATH = "/auth/login";
export const LOGOUT_PATH = "/auth/logout";

type SessionLike = { user?: { name?: unknown } } | null | undefined;

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
