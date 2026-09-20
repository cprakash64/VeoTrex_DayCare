"use client";

import { useEffect, useState } from "react";

import type { CoverageState, MonitoringStatus, SafetyEvent } from "../../lib/demo-runtime";
import {
  clockTime,
  coverageTone,
  eventTitle,
  metricText as metric,
  occupancyDisplay,
  sourceBadge,
  systemStatus,
  thresholdDisplay,
} from "../../lib/monitoring-presentation";

const POLL_INTERVAL_MS = 1_000;

/* Presentation helpers ------------------------------------------------------------------ */

export function SourceBadge({ kind }: { kind: string }) {
  // A recording is never allowed to render as a live camera. The two states have different
  // words and different colours precisely so nobody watching can confuse them.
  const view = sourceBadge(kind);
  return (
    <span className={`badge badge--${view.tone}`}>
      <span className="badge__dot" aria-hidden="true" />
      {view.label}
    </span>
  );
}

function CoverageBadge({ state }: { state: CoverageState }) {
  return <span className={`badge badge--${coverageTone(state)}`}>{state}</span>;
}

/* Live polling --------------------------------------------------------------------------- */

export function useMonitoringStatus(initial: MonitoringStatus): MonitoringStatus {
  const [status, setStatus] = useState(initial);
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const response = await fetch("/api/demo/state", { cache: "no-store" });
        if (!response.ok) throw new Error("unavailable");
        const next = (await response.json()) as MonitoringStatus;
        if (!cancelled) setStatus(next);
      } catch {
        // A failed poll means the runtime is not reachable right now, which is a real state
        // worth showing - not a reason to keep displaying the last good numbers.
        if (!cancelled) setStatus({ available: false, reason: "unreachable" });
      }
    };
    const timer = setInterval(tick, POLL_INTERVAL_MS);
    void tick();
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);
  return status;
}

/* Panels ---------------------------------------------------------------------------------- */

function CameraPanel({ status }: { status: MonitoringStatus }) {
  return (
    <section className="card card--flush" aria-label="Camera">
      <div className="viewport">
        {status.available ? (
          // eslint-disable-next-line @next/next/no-img-element -- MJPEG needs a raw <img>
          <img src="/api/demo/stream" alt="Monitored area with detected people outlined" />
        ) : (
          <div className="viewport__empty">
            <strong>No monitoring source connected</strong>
            <span>
              {status.reason === "not_configured"
                ? "No monitoring runtime is configured for this environment."
                : "The monitoring runtime is not reachable. Safety state is unknown."}
            </span>
          </div>
        )}
        <div className="viewport__overlay">
          <div className="viewport__row">
            <span className="viewport__title">
              {status.available ? status.state.area.label : "Not monitoring"}
            </span>
            {status.available ? <SourceBadge kind={status.state.source.kind} /> : null}
          </div>
          {status.available ? (
            <div className="viewport__row viewport__row--foot">
              <span className="badge badge--chip">
                {metric(status.state.telemetry.measured_fps)} fps
              </span>
              <span className="badge badge--chip">
                {metric(status.state.telemetry.inference_latency_ms, " ms")} inference
              </span>
              <span className="badge badge--chip">
                {metric(status.state.telemetry.active_tracks)} tracks
              </span>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function ClassroomStatus({ status }: { status: MonitoringStatus }) {
  const occupancy = status.available ? status.state.occupancy : null;
  const threshold = status.available ? status.state.demo_threshold : null;
  const view = occupancyDisplay(
    occupancy?.certainty ?? "UNKNOWN",
    occupancy?.people_detected ?? null,
  );
  const thresholdView = threshold ? thresholdDisplay(threshold.state) : null;
  return (
    <section className="card" aria-label="Classroom status">
      <h2>Classroom status</h2>
      <div className="metric">
        <span className="metric__label">People detected</span>
        <span className={`metric__value${view.measured ? "" : " metric__value--unknown"}`}>
          {view.text}
        </span>
      </div>
      {thresholdView?.shown ? (
        <div className="metric">
          <span className="metric__label">Demo threshold</span>
          <span className="metric__value metric__value--sm">
            <span className={`badge badge--${thresholdView.tone}`}>{thresholdView.label}</span>
          </span>
        </div>
      ) : null}
      <p className="note">
        People detected is a count of distinct people currently tracked. VeoTrex does not
        estimate age, identity or role from imagery, so it does not separate staff from
        children.{" "}
        {threshold && threshold.permitted_people !== null
          ? `The threshold of ${threshold.permitted_people} is a configured demonstration value, not a regulatory ratio.`
          : ""}
      </p>
    </section>
  );
}

function CameraHealth({ status }: { status: MonitoringStatus }) {
  const coverage: CoverageState = status.available ? status.state.coverage.state : "UNKNOWN";
  const health = status.available ? status.state.source.health : "UNAVAILABLE";
  const online = health === "RUNNING";
  return (
    <section className="card" aria-label="Camera health">
      <h2>Camera health</h2>
      <div className="rows">
        <div className="row">
          <span className="row__label">Camera</span>
          <span className={`badge ${online ? "badge--ok" : "badge--unknown"}`}>
            {online ? "Online" : "Offline"}
          </span>
        </div>
        <div className="row">
          <span className="row__label">Monitoring coverage</span>
          <CoverageBadge state={coverage} />
        </div>
        <div className="row">
          <span className="row__label">Edge inference</span>
          <span className={`badge ${online ? "badge--ok" : "badge--unknown"}`}>
            {online ? "Running" : "Unknown"}
          </span>
        </div>
        {status.available ? (
          <div className="row">
            <span className="row__label">Last frame</span>
            <span className="metric__value metric__value--sm">
              {metric(status.state.coverage.seconds_since_last_frame, " s ago")}
            </span>
          </div>
        ) : null}
      </div>
      {coverage !== "ACTIVE" ? (
        <p className="note">
          Safety state is <strong>unknown</strong> while coverage is not active. Loss of video
          is never reported as an empty room.
        </p>
      ) : null}
    </section>
  );
}

export function EventTimeline({ events }: { events: ReadonlyArray<SafetyEvent> }) {
  if (events.length === 0) {
    return (
      <div className="empty">
        <strong>No safety events during this session.</strong>
        Events appear here when the running pipeline observes a real state change.
      </div>
    );
  }
  return (
    <ul className="timeline">
      {events.map((event) => (
        <li key={`${event.sequence}-${event.kind}`}>
          <span className="timeline__time">{clockTime(event.occurred_at)}</span>
          <span>
            <span className="timeline__title">{eventTitle(event.kind)}</span>
            <span className="timeline__meta">
              {event.area_label}
              {event.duration_seconds !== null ? ` · ${event.duration_seconds}s` : ""}
              {event.people_detected !== null ? ` · ${event.people_detected} detected` : ""}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/* Page body -------------------------------------------------------------------------------- */

export function SystemStatusBadge({ status }: { status: MonitoringStatus }) {
  const view = systemStatus(status);
  return (
    <span className={`badge badge--${view.tone}`}>
      {view.tone === "ok" ? <span className="badge__dot" aria-hidden="true" /> : null}
      {view.label}
    </span>
  );
}

export function SafetyOperations({ initial }: { initial: MonitoringStatus }) {
  const status = useMonitoringStatus(initial);
  return (
    <>
      <div className="app-head">
        <div>
          <h1>Safety Operations</h1>
          <p>Real-time classroom monitoring and safety intelligence</p>
        </div>
        <SystemStatusBadge status={status} />
      </div>
      <div className="ops-grid">
        <CameraPanel status={status} />
        <div className="ops-side">
          <ClassroomStatus status={status} />
          <CameraHealth status={status} />
        </div>
      </div>
      <div className="ops-lower">
        <section className="card" aria-label="Recent safety events">
          <h2>Recent safety events</h2>
          <EventTimeline events={status.available ? status.state.events : []} />
        </section>
        <section className="card" aria-label="Processing">
          <h2>Processing</h2>
          <div className="rows">
            <div className="row">
              <span className="row__label">Frames processed</span>
              <span className="metric__value metric__value--sm">
                {status.available ? status.state.telemetry.frames_processed : "—"}
              </span>
            </div>
            <div className="row">
              <span className="row__label">Decode failures</span>
              <span className="metric__value metric__value--sm">
                {status.available ? status.state.telemetry.decode_failures : "—"}
              </span>
            </div>
            <div className="row">
              <span className="row__label">Detection and tracking</span>
              <span className="badge badge--muted">On device</span>
            </div>
          </div>
          <p className="note">
            Video is decoded and analysed on the edge device. Frames are not sent to a cloud
            inference service.
          </p>
        </section>
      </div>
    </>
  );
}
