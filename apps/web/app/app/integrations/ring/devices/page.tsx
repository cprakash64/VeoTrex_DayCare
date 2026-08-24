import { redirect } from "next/navigation";

import { auth0 } from "../../../../../lib/auth0";
import { providerStatus } from "../../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../../lib/session-policy";
import { getApplicationIdentity, getRingInventory } from "../../../../../lib/veotrex-api";
import { SyncButton } from "./sync-button";

export default async function RingDevicesPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [identity, cameras] = await Promise.all([getApplicationIdentity(), getRingInventory()]);
  const canSynchronize = identity.permissions.includes("manage:integrations");
  const connectionIds = [...new Set(cameras.map((camera) => camera.connection_id))];

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Ring</p>
          <h1>Device inventory</h1>
        </div>
        <a href="/app">Back</a>
      </header>
      <p>
        Discovery does not assign a camera to a room. Physical assignment remains an explicit
        VeoTrex administrator action.
      </p>
      {canSynchronize ? connectionIds.map((id) => <SyncButton key={id} connectionId={id} />) : null}
      {cameras.length === 0 ? (
        <section><h2>No Ring cameras discovered</h2><p>Connect and synchronize a Ring account.</p></section>
      ) : (
        <section className="inventory-grid" aria-label="Ring camera inventory">
          {cameras.map((camera) => (
            <article className="camera-card" key={camera.camera_id}>
              <p className="eyebrow">{camera.provider}</p>
              <h2>{camera.display_name}</h2>
              <p>{providerStatus(camera.provider_online)} · {camera.assigned ? "Assigned" : "Discovered, unassigned"}</p>
              <p>{camera.capabilities.length ? camera.capabilities.join(" · ") : "No camera capabilities reported"}</p>
              {camera.privacy_controls_configured ? <p>Ring privacy controls configured</p> : null}
              <p>
                {camera.last_synchronized_at
                  ? `Last synchronized ${new Date(camera.last_synchronized_at).toLocaleString()}`
                  : "Not synchronized yet"}
              </p>
              <small>Camera {camera.camera_id}</small>
            </article>
          ))}
        </section>
      )}
    </main>
  );
}
