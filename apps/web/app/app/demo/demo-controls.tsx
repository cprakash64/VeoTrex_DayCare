"use client";

import { useState } from "react";

import type { MonitoringStatus } from "../../../lib/demo-runtime";
import { useMonitoringStatus } from "../monitoring-view";

type Outcome = { tone: "ok" | "alert"; message: string } | null;

/**
 * Operator controls for driving a recording.
 *
 * Restart only. Pause is deliberately absent: with no frames arriving the pipeline would
 * correctly degrade to impaired coverage and unknown occupancy, which is the right safety
 * behaviour and the wrong thing to trigger on purpose mid-demo.
 */
export function DemoControls({ initial }: { initial: MonitoringStatus }) {
  const status = useMonitoringStatus(initial);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  async function restart() {
    setBusy(true);
    setOutcome(null);
    try {
      const response = await fetch("/api/demo/restart", { method: "POST" });
      setOutcome(
        response.ok
          ? { tone: "ok", message: "Recording restarted from the beginning." }
          : { tone: "alert", message: "The monitoring runtime did not accept the restart." },
      );
    } catch {
      setOutcome({ tone: "alert", message: "The monitoring runtime is unreachable." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className="card" aria-label="Pipeline state">
        <h2>Pipeline state</h2>
        <div className="rows">
          <div className="row">
            <span className="row__label">Runtime</span>
            <span className={`badge badge--${status.available ? "ok" : "unknown"}`}>
              {status.available ? "Reachable" : "Unavailable"}
            </span>
          </div>
          <div className="row">
            <span className="row__label">Source kind</span>
            <span className="badge badge--muted">
              {status.available ? status.state.source.kind : "—"}
            </span>
          </div>
          <div className="row">
            <span className="row__label">Source health</span>
            <span className="badge badge--muted">
              {status.available ? status.state.source.health : "—"}
            </span>
          </div>
          <div className="row">
            <span className="row__label">Loops completed</span>
            <span className="metric__value metric__value--sm">
              {status.available ? status.state.source.loops_completed : "—"}
            </span>
          </div>
          <div className="row">
            <span className="row__label">Position in clip</span>
            <span className="metric__value metric__value--sm">
              {status.available && status.state.source.media_timestamp_seconds !== null
                ? `${status.state.source.media_timestamp_seconds.toFixed(1)} s`
                : "—"}
            </span>
          </div>
          <div className="row">
            <span className="row__label">Frames processed</span>
            <span className="metric__value metric__value--sm">
              {status.available ? status.state.telemetry.frames_processed : "—"}
            </span>
          </div>
        </div>
      </section>

      <section className="card" aria-label="Playback" style={{ marginTop: "1rem" }}>
        <h2>Playback</h2>
        <button
          className="primary"
          type="button"
          onClick={restart}
          disabled={busy || !status.available}
        >
          {busy ? "Restarting…" : "Restart recording"}
        </button>
        {outcome ? (
          <p
            className="note"
            role={outcome.tone === "alert" ? "alert" : undefined}
            style={outcome.tone === "alert" ? { color: "#ff9c8f" } : undefined}
          >
            {outcome.message}
          </p>
        ) : null}
        <p className="note">
          Looping is configured on the runtime with <code>VEOTREX_DEMO_VIDEO_LOOP</code>. The
          source, clip and classroom labels are runtime configuration, so switching between a
          recorded source and a camera is a restart of the runtime rather than a control here.
        </p>
      </section>
    </>
  );
}
