import { redirect } from "next/navigation";

import { auth0 } from "../../lib/auth0";
import { protectedRouteRedirect } from "../../lib/session-policy";
import { getApplicationIdentity } from "../../lib/veotrex-api";

export default async function ApplicationShell() {
  const session = await auth0.getSession();
  const destination = protectedRouteRedirect(session);
  if (destination) {
    redirect(destination);
  }
  const identity = await getApplicationIdentity();

  return (
    <main>
      <header className="toolbar">
        <p className="eyebrow">VeoTrex</p>
        <a href="/auth/logout">Log out</a>
      </header>
      <h1>Safety operations</h1>
      <p>Signed in as {identity.display_name ?? "authorized member"}.</p>
      <section aria-labelledby="access-heading">
        <h2 id="access-heading">Your access</h2>
        <p>{identity.roles.map(({ role }) => role.replaceAll("_", " ")).join(", ")}</p>
      </section>
    </main>
  );
}
