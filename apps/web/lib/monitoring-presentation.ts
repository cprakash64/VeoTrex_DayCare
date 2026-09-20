/**
 * Pure presentation rules for monitoring state.
 *
 * These are the decisions that have to be right for the product to be honest - whether a
 * source reads as live, whether a missing measurement becomes a number, what a lost camera
 * says about occupancy - so they live outside React where they can be tested directly.
 *
 * Type-only imports from the server-only runtime client: no server code is pulled into the
 * browser bundle.
 */

import type {
  CoverageState,
  MonitoringStatus,
  OccupancyCertainty,
  ThresholdState,
} from "./demo-runtime";

export type Tone = "ok" | "warn" | "alert" | "unknown" | "muted";

export type SourceBadgeView = Readonly<{ label: string; tone: "live" | "recorded" }>;

/** Anything that is not an explicitly live provider reads as recorded. Fail safe, not live. */
export function sourceBadge(kind: string): SourceBadgeView {
  return kind === "LIVE_RING"
    ? { label: "Live • Ring", tone: "live" }
    : { label: "Demo • Recorded", tone: "recorded" };
}

export function coverageTone(state: CoverageState): Tone {
  if (state === "ACTIVE") return "ok";
  if (state === "IMPAIRED") return "warn";
  return "unknown";
}

export type OccupancyView = Readonly<{ text: string; measured: boolean }>;

/**
 * Losing coverage yields "Unknown", never "0". This is the single most important rule in
 * the product: an empty screen is not an empty room.
 */
export function occupancyDisplay(
  certainty: OccupancyCertainty,
  peopleDetected: number | null,
): OccupancyView {
  if (certainty !== "MEASURED" || peopleDetected === null) {
    return { text: "Unknown", measured: false };
  }
  return { text: String(peopleDetected), measured: true };
}

/** An unmeasured metric renders as an em dash. Never a zero, never a plausible placeholder. */
export function metricText(value: number | null | undefined, suffix = ""): string {
  return value === null || value === undefined ? "—" : `${value}${suffix}`;
}

export type ThresholdView = Readonly<{ label: string; tone: Tone; shown: boolean }>;

export function thresholdDisplay(state: ThresholdState): ThresholdView {
  switch (state) {
    case "WITHIN_THRESHOLD":
      return { label: "Within threshold", tone: "ok", shown: true };
    case "OVER_THRESHOLD":
      return { label: "Over threshold", tone: "alert", shown: true };
    case "UNKNOWN":
      return { label: "Unknown", tone: "unknown", shown: true };
    default:
      return { label: "Not configured", tone: "muted", shown: false };
  }
}

export type SystemStatusView = Readonly<{ label: string; tone: Tone }>;

export function systemStatus(status: MonitoringStatus): SystemStatusView {
  if (!status.available) return { label: "Monitoring unavailable", tone: "unknown" };
  return status.state.coverage.state === "ACTIVE"
    ? { label: "Monitoring active", tone: "ok" }
    : { label: "Coverage impaired", tone: "warn" };
}

export function eventTitle(kind: string): string {
  switch (kind) {
    case "MONITORING_COVERAGE_LOST":
      return "Monitoring coverage lost";
    case "MONITORING_COVERAGE_RESTORED":
      return "Monitoring coverage restored";
    case "DEMO_THRESHOLD_EXCEEDED":
      return "Demo occupancy threshold exceeded";
    case "DEMO_THRESHOLD_CLEARED":
      return "Demo occupancy threshold cleared";
    default:
      return kind.replaceAll("_", " ").toLowerCase();
  }
}

export function clockTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime())
    ? "—"
    : parsed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
