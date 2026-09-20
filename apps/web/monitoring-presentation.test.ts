import { describe, expect, it } from "vitest";

import type { MonitoringStatus } from "./lib/demo-runtime";
import {
  clockTime,
  coverageTone,
  eventTitle,
  metricText,
  occupancyDisplay,
  sourceBadge,
  systemStatus,
  thresholdDisplay,
} from "./lib/monitoring-presentation";

function status(overrides: Partial<MonitoringStatus> = {}): MonitoringStatus {
  return {
    available: true,
    state: {
      source: {
        kind: "RECORDED_DEMO",
        health: "RUNNING",
        is_live: false,
        loops_completed: 0,
        media_timestamp_seconds: 1.2,
        error_category: null,
      },
      area: { label: "Demo Classroom", camera_label: "Demo Camera" },
      coverage: { state: "ACTIVE", seconds_since_last_frame: 0.2 },
      occupancy: { certainty: "MEASURED", people_detected: 4, confirmed_track_ids: [1, 2, 3, 4] },
      demo_threshold: { state: "WITHIN_THRESHOLD", permitted_people: 4 },
      telemetry: {
        measured_fps: 9.8,
        inference_latency_ms: 21.4,
        active_tracks: 4,
        frames_processed: 120,
        decode_failures: 0,
      },
      events: [],
    },
    ...overrides,
  } as MonitoringStatus;
}

describe("source labelling", () => {
  it("labels a Ring source as live", () => {
    expect(sourceBadge("LIVE_RING")).toEqual({ label: "Live • Ring", tone: "live" });
  });

  it("labels a recorded source as recorded", () => {
    expect(sourceBadge("RECORDED_DEMO")).toEqual({ label: "Demo • Recorded", tone: "recorded" });
  });

  it("treats an unrecognised source as recorded rather than live", () => {
    // Fail safe: an unknown provider must never be presented as a live camera.
    expect(sourceBadge("SOMETHING_ELSE").tone).toBe("recorded");
    expect(sourceBadge("").tone).toBe("recorded");
  });
});

describe("occupancy display", () => {
  it("shows a measured count", () => {
    expect(occupancyDisplay("MEASURED", 4)).toEqual({ text: "4", measured: true });
  });

  it("shows zero when zero people were genuinely measured", () => {
    expect(occupancyDisplay("MEASURED", 0)).toEqual({ text: "0", measured: true });
  });

  it("shows Unknown rather than zero when the count is not measured", () => {
    expect(occupancyDisplay("UNKNOWN", null)).toEqual({ text: "Unknown", measured: false });
  });

  it("refuses a stale count that arrives with an unknown certainty", () => {
    expect(occupancyDisplay("UNKNOWN", 4).text).toBe("Unknown");
  });
});

describe("coverage tone", () => {
  it("is affirmative only while coverage is active", () => {
    expect(coverageTone("ACTIVE")).toBe("ok");
    expect(coverageTone("IMPAIRED")).toBe("warn");
    expect(coverageTone("UNKNOWN")).toBe("unknown");
  });
});

describe("metric rendering", () => {
  it("renders a measured value with its unit", () => {
    expect(metricText(9.8, " fps")).toBe("9.8 fps");
    expect(metricText(0)).toBe("0");
  });

  it("renders absence as an em dash, never as a number", () => {
    expect(metricText(null)).toBe("—");
    expect(metricText(undefined, " ms")).toBe("—");
  });
});

describe("demo threshold", () => {
  it("is hidden entirely when no staffing is configured", () => {
    expect(thresholdDisplay("NOT_CONFIGURED").shown).toBe(false);
  });

  it("reports unknown rather than compliant when the count is unknown", () => {
    expect(thresholdDisplay("UNKNOWN")).toEqual({
      label: "Unknown",
      tone: "unknown",
      shown: true,
    });
  });

  it("distinguishes within and over threshold", () => {
    expect(thresholdDisplay("WITHIN_THRESHOLD").tone).toBe("ok");
    expect(thresholdDisplay("OVER_THRESHOLD").tone).toBe("alert");
  });
});

describe("system status badge", () => {
  it("is active only when the runtime is reachable and covering", () => {
    expect(systemStatus(status())).toEqual({ label: "Monitoring active", tone: "ok" });
  });

  it("is impaired when coverage drops", () => {
    const base = status();
    if (!base.available) throw new Error("fixture must be available");
    const next: MonitoringStatus = {
      available: true,
      state: {
        ...base.state,
        coverage: { state: "IMPAIRED", seconds_since_last_frame: 12 },
      },
    };
    expect(systemStatus(next)).toEqual({ label: "Coverage impaired", tone: "warn" });
  });

  it("never claims online when the runtime is unreachable", () => {
    expect(systemStatus({ available: false, reason: "unreachable" })).toEqual({
      label: "Monitoring unavailable",
      tone: "unknown",
    });
    expect(systemStatus({ available: false, reason: "not_configured" }).tone).toBe("unknown");
  });
});

describe("event presentation", () => {
  it("names the events the pipeline actually emits", () => {
    expect(eventTitle("MONITORING_COVERAGE_LOST")).toBe("Monitoring coverage lost");
    expect(eventTitle("DEMO_THRESHOLD_EXCEEDED")).toBe("Demo occupancy threshold exceeded");
  });

  it("degrades gracefully for an unrecognised kind", () => {
    expect(eventTitle("SOME_NEW_KIND")).toBe("some new kind");
  });

  it("renders an unparseable timestamp as an em dash", () => {
    expect(clockTime("not-a-date")).toBe("—");
  });
});
