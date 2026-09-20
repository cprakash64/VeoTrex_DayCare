import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { fetchMonitoringStatus } from "../../../lib/demo-runtime";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { EventTimeline } from "../monitoring-view";

export const dynamic = "force-dynamic";

export default async function SafetyEventsPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const monitoring = await fetchMonitoringStatus();

  return (
    <>
      <div className="app-head">
        <div>
          <h1>Safety Events</h1>
          <p>State changes observed by the monitoring pipeline</p>
        </div>
      </div>
      <section className="card" aria-label="Safety events">
        <h2>This session</h2>
        <EventTimeline events={monitoring.available ? monitoring.state.events : []} />
        <p className="note">
          Events are produced by the running pipeline when it observes a real change, such as
          coverage being lost or a configured demonstration threshold being crossed. They are
          held for the life of the monitoring process; there is no durable safety-event record
          in the control plane yet, so this list is not an audit trail.
        </p>
      </section>
    </>
  );
}
