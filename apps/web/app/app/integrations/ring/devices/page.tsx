import { redirect } from "next/navigation";

import { auth0 } from "../../../../../lib/auth0";
import { connectionPanels, providerStatus } from "../../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../../lib/session-policy";
import {
  getApplicationIdentity,
  getRingConnections,
  getRingInventory,
  type RingInventoryCamera,
} from "../../../../../lib/veotrex-api";
import { SyncButton } from "./sync-button";

export default async function RingDevicesPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [identity, connections, cameras] = await Promise.all([
    getApplicationIdentity(),
    getRingConnections(),
    getRingInventory(),
  ]);
  const canSynchronize = identity.permissions.includes("manage:integrations");
  const panels = connectionPanels(connections, cameras);

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
      {panels.length === 0 ? (
        <section>
          <h2>No Ring account connected</h2>
          <p>Connect a Ring account from the Ring app, then synchronize its devices here.</p>
        </section>
      ) : (
        panels.map((panel) => (
          <section
            className="connection-panel"
            key={panel.connectionId}
            aria-label={`Ring connection ${panel.displayName}`}
          >
            <h2>{panel.displayName}</h2>
            <p>{panel.statusLabel}</p>
            {panel.canSync && canSynchronize ? (
              <SyncButton connectionId={panel.connectionId} />
            ) : null}
            {panel.cameras.length === 0 ? (
              <p>
                {panel.neverSynchronized
                  ? "Connected, but the device inventory has not been synchronized yet."
                  : "No Ring cameras discovered on this connection."}
              </p>
            ) : (
              <div className="inventory-grid" aria-label="Ring camera inventory">
                {panel.cameras.map((camera) => (
                  <CameraCard key={camera.camera_id} camera={camera} />
                ))}
              </div>
            )}
          </section>
        ))
      )}
    </main>
  );
}

function CameraCard({ camera }: { camera: RingInventoryCamera }) {
  return (
    <article className="camera-card">
      <p className="eyebrow">{camera.provider}</p>
      <h3>{camera.display_name}</h3>
      <p>{providerStatus(camera.provider_online)} · {camera.assigned ? "Assigned" : "Discovered, unassigned"}</p>
      <p>{camera.capabilities.length ? camera.capabilities.join(" · ") : "No camera capabilities reported"}</p>
      {camera.privacy_controls_configured ? <p>Ring privacy controls configured</p> : null}
      <p>
        {camera.last_synchronized_at
          ? `Last synchronized ${new Date(camera.last_synchronized_at).toLocaleString()}`
          : "Not synchronized yet"}
      </p>
    </article>
  );
}
