import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { fetchMonitoringStatus } from "../../../lib/demo-runtime";
import { protectedRouteRedirect } from "../../../lib/session-policy";

export const dynamic = "force-dynamic";

export default async function ClassroomsPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const monitoring = await fetchMonitoringStatus();
  const threshold = monitoring.available ? monitoring.state.demo_threshold : null;

  return (
    <>
      <div className="app-head">
        <div>
          <h1>Classrooms</h1>
          <p>Monitored areas and their configured demonstration thresholds</p>
        </div>
      </div>
      <section className="card" aria-label="Monitored classrooms">
        <h2>Monitored</h2>
        {monitoring.available ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Classroom</th>
                  <th scope="col">Camera</th>
                  <th scope="col">People detected</th>
                  <th scope="col">Demo threshold</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{monitoring.state.area.label}</td>
                  <td>{monitoring.state.area.camera_label}</td>
                  <td>
                    {monitoring.state.occupancy.certainty === "MEASURED"
                      ? monitoring.state.occupancy.people_detected
                      : "Unknown"}
                  </td>
                  <td>
                    {threshold?.permitted_people === null || threshold === null
                      ? "Not configured"
                      : `${threshold.permitted_people} people`}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            <strong>No classroom is being monitored.</strong>
            Start the monitoring runtime to analyse a classroom.
          </div>
        )}
        <p className="note">
          The demonstration threshold is a value the operator configures for this runtime. It
          is not a jurisdictional staffing ratio, and VeoTrex does not determine who is a
          staff member from imagery. Classroom records in the control plane are not yet linked
          to the monitoring pipeline, so this reflects the running runtime&rsquo;s
          configuration.
        </p>
      </section>
    </>
  );
}
