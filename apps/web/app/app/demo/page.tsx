import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { fetchMonitoringStatus } from "../../../lib/demo-runtime";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { DemoControls } from "./demo-controls";

export const dynamic = "force-dynamic";

/**
 * Operator-only view for running a recording session. Deliberately absent from the primary
 * navigation - it is reached by URL - but it is behind the same session guard as every other
 * destination under /app. Nothing here bypasses authentication.
 */
export default async function DemoOperatorPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const initial = await fetchMonitoringStatus();

  return (
    <>
      <div className="app-head">
        <div>
          <h1>Demo operator</h1>
          <p>Controls for running a recorded demonstration</p>
        </div>
        <span className="badge badge--recorded">Operator only</span>
      </div>
      <DemoControls initial={initial} />
    </>
  );
}
