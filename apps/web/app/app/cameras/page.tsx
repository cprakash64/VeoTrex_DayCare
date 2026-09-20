import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { fetchMonitoringStatus } from "../../../lib/demo-runtime";
import { getRingInventory, type RingInventoryCamera } from "../../../lib/veotrex-api";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { SourceBadge } from "../monitoring-view";

export const dynamic = "force-dynamic";

export default async function CamerasPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);

  const monitoring = await fetchMonitoringStatus();
  let inventory: ReadonlyArray<RingInventoryCamera> = [];
  let inventoryFailed = false;
  try {
    inventory = await getRingInventory();
  } catch {
    inventoryFailed = true;
  }

  return (
    <>
      <div className="app-head">
        <div>
          <h1>Cameras</h1>
          <p>Monitoring sources and linked provider devices</p>
        </div>
        <a className="secondary" href="/app/integrations/ring/devices">
          Manage Ring inventory
        </a>
      </div>

      <section className="card" aria-label="Active monitoring source">
        <h2>Active monitoring source</h2>
        {monitoring.available ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Area</th>
                  <th scope="col">Camera</th>
                  <th scope="col">Source</th>
                  <th scope="col">Coverage</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{monitoring.state.area.label}</td>
                  <td>{monitoring.state.area.camera_label}</td>
                  <td>
                    <SourceBadge kind={monitoring.state.source.kind} />
                  </td>
                  <td>{monitoring.state.coverage.state}</td>
                </tr>
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            <strong>No monitoring source is running.</strong>
            Start the monitoring runtime to analyse a camera or a recorded source.
          </div>
        )}
      </section>

      <section className="card" aria-label="Ring devices" style={{ marginTop: "1rem" }}>
        <h2>Ring devices</h2>
        {inventoryFailed ? (
          <div className="empty">
            <strong>Ring inventory is unavailable.</strong>
            The control plane did not return device inventory for this tenant.
          </div>
        ) : inventory.length === 0 ? (
          <div className="empty">
            <strong>No Ring cameras linked.</strong>
            Link a Ring account to see provider devices here.
          </div>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Device</th>
                  <th scope="col">State</th>
                  <th scope="col">Provider status</th>
                  <th scope="col">Assigned</th>
                </tr>
              </thead>
              <tbody>
                {inventory.map((camera) => (
                  <tr key={camera.camera_id}>
                    <td>{camera.display_name}</td>
                    <td>{camera.inventory_state}</td>
                    <td>
                      {camera.provider_online === null ? (
                        <span className="badge badge--unknown">Unknown</span>
                      ) : camera.provider_online ? (
                        <span className="badge badge--ok">Online</span>
                      ) : (
                        <span className="badge badge--warn">Offline</span>
                      )}
                    </td>
                    <td>{camera.assigned ? "Yes" : "No"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="note">
          Linked Ring devices are discovered through the provider inventory. Live Ring frames
          are not yet routed into on-device detection, so a Ring device listed here is not
          being analysed.
        </p>
      </section>
    </>
  );
}
