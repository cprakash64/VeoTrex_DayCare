import { auth0 } from "../lib/auth0";
import { landingActions } from "../lib/session-policy";

export default async function Home() {
  const actions = landingActions(await auth0.getSession());
  return (
    <main>
      <p className="eyebrow">VeoTrex</p>
      <h1>Childcare safety operations</h1>
      <p>A protected control plane for authorized childcare safety teams.</p>
      <nav aria-label="Account">
        {actions.map((action) => (
          // A plain anchor, never a prefetching Link: prefetch would start an Auth0
          // transaction merely because the link entered the viewport.
          <a
            key={action.href}
            className={action.emphasis === "plain" ? undefined : action.emphasis}
            href={action.href}
          >
            {action.label}
          </a>
        ))}
      </nav>
    </main>
  );
}
