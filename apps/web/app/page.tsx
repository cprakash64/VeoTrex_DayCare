import { auth0 } from "../lib/auth0";
import { LOGIN_PATH, LOGOUT_PATH, safeSessionView } from "../lib/session-policy";

export default async function Home() {
  const view = safeSessionView(await auth0.getSession());
  return (
    <main>
      <p className="eyebrow">VeoTrex</p>
      <h1>Childcare safety operations</h1>
      <p>A protected control plane for authorized childcare safety teams.</p>
      {view.authenticated ? (
        <nav aria-label="Account">
          <a className="primary" href="/app">Open VeoTrex</a>
          <a href={LOGOUT_PATH}>Log out</a>
        </nav>
      ) : (
        <a className="primary" href={LOGIN_PATH}>Log in</a>
      )}
    </main>
  );
}
