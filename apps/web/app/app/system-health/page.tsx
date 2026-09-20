import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { demoRuntimeBaseUrl, fetchMonitoringStatus } from "../../../lib/demo-runtime";
import { protectedRouteRedirect } from "../../../lib/session-policy";

export const dynamic = "force-dynamic";

export default async function SystemHealthPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const monitoring = await fetchMonitoringStatus();
  const configured = demoRuntimeBaseUrl() !== null;

  const rows: ReadonlyArray<[string, string, "ok" | "warn" | "unknown"]> = [
    [
      "Monitoring runtime",
      monitoring.available ? "Reachable" : configured ? "Unreachable" : "Not configured",
      monitoring.available ? "ok" : "unknown",
    ],
    [
      "Frame source",
      monitoring.available ? monitoring.state.source.health : "Unknown",
      monitoring.available && monitoring.state.source.health === "RUNNING" ? "ok" : "unknown",
    ],
    [
      "Monitoring coverage",
      monitoring.available ? monitoring.state.coverage.state : "UNKNOWN",
      monitoring.available && monitoring.state.coverage.state === "ACTIVE" ? "ok" : "warn",
    ],
    [
      "Measured throughput",
      monitoring.available && monitoring.state.telemetry.measured_fps !== null
        ? `${monitoring.state.telemetry.measured_fps} fps`
        : "—",
      "ok",
    ],
    [
      "Inference latency",
      monitoring.available && monitoring.state.telemetry.inference_latency_ms !== null
        ? `${monitoring.state.telemetry.inference_latency_ms} ms`
        : "—",
      "ok",
    ],
  ];

  return (
    <>
      <div className="app-head">
        <div>
          <h1>System Health</h1>
          <p>Edge processing and monitoring coverage</p>
        </div>
      </div>
      <section className="card" aria-label="System health">
        <h2>Components</h2>
        <div className="rows">
          {rows.map(([label, value, tone]) => (
            <div className="row" key={label}>
              <span className="row__label">{label}</span>
              <span className={`badge badge--${tone}`}>{value}</span>
            </div>
          ))}
        </div>
        {monitoring.available && monitoring.state.source.error_category ? (
          <p className="note">
            Last source error: <strong>{monitoring.state.source.error_category}</strong>
          </p>
        ) : null}
        <p className="note">
          Detection and tracking run on the edge device. Control-plane health, database state
          and backup status are not reported on this page.
        </p>
      </section>
    </>
  );
}
