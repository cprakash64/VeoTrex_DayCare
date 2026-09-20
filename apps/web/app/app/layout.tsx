import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { auth0 } from "../../lib/auth0";
import { protectedRouteRedirect } from "../../lib/session-policy";
import { getApplicationIdentity } from "../../lib/veotrex-api";
import { NavLink } from "./nav-link";

/**
 * Protected application shell.
 *
 * The session check is repeated here rather than left to the individual pages: a layout is
 * the one place every destination under /app passes through, and the pages keep their own
 * guards as well so neither is load-bearing on its own.
 */
export default async function ApplicationLayout({ children }: { children: ReactNode }) {
  const session = await auth0.getSession();
  const destination = protectedRouteRedirect(session);
  if (destination) {
    redirect(destination);
  }
  const identity = await getApplicationIdentity();
  const roles = identity.roles.map(({ role }) => role.replaceAll("_", " ")).join(", ");

  return (
    <main className="app-shell">
      <nav className="app-nav" aria-label="Primary">
        <div className="app-nav__brand">
          <span className="app-nav__mark" aria-hidden="true">
            V
          </span>
          <span className="app-nav__name">VeoTrex</span>
        </div>
        <div className="app-nav__links">
          <NavLink href="/app">Overview</NavLink>
          <NavLink href="/app/cameras">Cameras</NavLink>
          <NavLink href="/app/safety-events">Safety Events</NavLink>
          <NavLink href="/app/classrooms">Classrooms</NavLink>
          <NavLink href="/app/system-health">System Health</NavLink>
        </div>
        <div className="app-nav__footer">
          <div className="app-nav__account">
            <strong>{identity.display_name ?? "Authorized member"}</strong>
            {roles || "No roles assigned"}
          </div>
          <a className="app-nav__signout" href="/auth/logout">
            Sign out
          </a>
        </div>
      </nav>
      <div className="app-main">{children}</div>
    </main>
  );
}
