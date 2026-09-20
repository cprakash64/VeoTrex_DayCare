import "server-only";

/**
 * Client for the device-local monitoring runtime.
 *
 * The runtime binds to loopback and has no authentication of its own, so the browser must
 * never reach it directly. Every call goes through a server-side route that has already
 * established an Auth0 session, which is why this module is server-only.
 *
 * Absence is preserved throughout: a metric the runtime reports as null stays null all the
 * way to the screen, and an unreachable runtime is `unavailable` rather than a zeroed state.
 */

export type CoverageState = "ACTIVE" | "IMPAIRED" | "UNKNOWN";
export type OccupancyCertainty = "MEASURED" | "UNKNOWN";
export type ThresholdState =
  | "WITHIN_THRESHOLD"
  | "OVER_THRESHOLD"
  | "UNKNOWN"
  | "NOT_CONFIGURED";
export type SourceKind = "RECORDED_DEMO" | "LIVE_RING";

export type SafetyEvent = Readonly<{
  sequence: number;
  kind: string;
  area_label: string;
  occurred_at: string;
  occurred_at_monotonic: number;
  people_detected: number | null;
  permitted_people: number | null;
  duration_seconds: number | null;
}>;

export type MonitoringState = Readonly<{
  source: Readonly<{
    kind: SourceKind;
    health: string;
    is_live: boolean;
    loops_completed: number;
    media_timestamp_seconds: number | null;
    error_category: string | null;
  }>;
  area: Readonly<{ label: string; camera_label: string }>;
  coverage: Readonly<{ state: CoverageState; seconds_since_last_frame: number | null }>;
  occupancy: Readonly<{
    certainty: OccupancyCertainty;
    people_detected: number | null;
    confirmed_track_ids: ReadonlyArray<number>;
  }>;
  demo_threshold: Readonly<{ state: ThresholdState; permitted_people: number | null }>;
  telemetry: Readonly<{
    measured_fps: number | null;
    inference_latency_ms: number | null;
    active_tracks: number | null;
    frames_processed: number;
    decode_failures: number;
  }>;
  events: ReadonlyArray<SafetyEvent>;
}>;

export type MonitoringStatus =
  | Readonly<{ available: true; state: MonitoringState }>
  | Readonly<{ available: false; reason: "not_configured" | "unreachable" }>;

const DEFAULT_TIMEOUT_MS = 2_000;

export function demoRuntimeBaseUrl(): string | null {
  const configured = process.env.VEOTREX_DEMO_RUNTIME_URL?.trim();
  return configured ? configured.replace(/\/$/, "") : null;
}

export async function fetchMonitoringStatus(): Promise<MonitoringStatus> {
  const baseUrl = demoRuntimeBaseUrl();
  if (!baseUrl) {
    return { available: false, reason: "not_configured" };
  }
  try {
    const response = await fetch(`${baseUrl}/state`, {
      cache: "no-store",
      signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    });
    if (!response.ok) {
      return { available: false, reason: "unreachable" };
    }
    return { available: true, state: (await response.json()) as MonitoringState };
  } catch {
    // A runtime that is not running is a legitimate state, not an error page. The dashboard
    // reports it as unknown coverage rather than pretending the room is empty.
    return { available: false, reason: "unreachable" };
  }
}
