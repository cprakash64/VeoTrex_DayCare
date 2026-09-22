import { describe, expect, it } from "vitest";

import {
  connectionPanels,
  providerStatus,
  UUID_PATTERN,
  type RingConnectionSummary,
} from "./lib/ring-inventory";

const ACTIVE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ACTIVE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function connection(overrides: Partial<RingConnectionSummary> = {}): RingConnectionSummary {
  return {
    connection_id: ACTIVE_A,
    display_name: "Ring account aaaaaaaa",
    status: "ACTIVE",
    integration_state: "ACTIVE",
    operational_health: "ACTIVE",
    last_synchronized_at: null,
    last_sync_failure_category: null,
    ...overrides,
  };
}

describe("Ring inventory presentation boundary", () => {
  it("shows provider status without implying stream or AI health", () => {
    expect(providerStatus(true)).toBe("Ring online");
    expect(providerStatus(false)).toBe("Ring offline");
    expect(providerStatus(null)).toBe("Ring status unknown");
  });

  it("accepts only internal UUID connection identifiers for the sync BFF", () => {
    expect(UUID_PATTERN.test("de305d54-75b4-431b-adb2-eb6b9e546014")).toBe(true);
    expect(UUID_PATTERN.test("../other-tenant")).toBe(false);
  });
});

describe("connection panels (V1-01A-1)", () => {
  it("case A: no connections renders no panel and therefore no sync control", () => {
    expect(connectionPanels([], [])).toEqual([]);
    // Cameras without a listed connection never invent one.
    expect(connectionPanels([], [{ camera_id: "c1", connection_id: ACTIVE_A }])).toEqual([]);
  });

  it("case B: an ACTIVE connection with ZERO cameras is visible and can sync", () => {
    const [panel] = connectionPanels([connection()], []);
    expect(panel.connectionId).toBe(ACTIVE_A);
    expect(panel.presentation).toBe("active");
    expect(panel.canSync).toBe(true);
    expect(panel.neverSynchronized).toBe(true);
    expect(panel.cameras).toEqual([]);
    expect(panel.statusLabel).toBe("Connected");
    // Nothing secret or provider-identifying exists to render.
    expect(Object.keys(panel).sort()).toEqual(
      ["cameras", "canSync", "connectionId", "displayName", "neverSynchronized", "presentation", "statusLabel"],
    );
  });

  it("case C: an ACTIVE connection keeps its cameras and can sync", () => {
    const cameras = [
      { camera_id: "c2", connection_id: ACTIVE_A },
      { camera_id: "c1", connection_id: ACTIVE_A },
    ];
    const [panel] = connectionPanels(
      [connection({ last_synchronized_at: "2026-09-22T00:00:00Z" })],
      cameras,
    );
    expect(panel.canSync).toBe(true);
    expect(panel.neverSynchronized).toBe(false);
    expect(panel.cameras.map((camera) => camera.camera_id)).toEqual(["c2", "c1"]);
  });

  it("case D: several eligible connections render deterministically, each with its own cameras", () => {
    const panels = connectionPanels(
      [
        connection({ connection_id: ACTIVE_B, display_name: "Ring account bbbbbbbb" }),
        connection(),
      ],
      [{ camera_id: "cb", connection_id: ACTIVE_B }],
    );
    expect(panels.map((panel) => panel.connectionId)).toEqual([ACTIVE_A, ACTIVE_B]);
    expect(panels[0].cameras).toEqual([]);
    expect(panels[1].cameras.map((camera) => camera.camera_id)).toEqual(["cb"]);
    expect(panels.every((panel) => panel.canSync)).toBe(true);
  });

  it("case E: DISCONNECTED and ARCHIVED connections never offer sync", () => {
    for (const state of ["DISCONNECTED", "ARCHIVED"]) {
      const [panel] = connectionPanels(
        [connection({ status: "DISABLED", integration_state: state })],
        [],
      );
      expect(panel.presentation).toBe("disconnected");
      expect(panel.canSync).toBe(false);
      expect(panel.statusLabel).toBe("Disconnected");
    }
  });

  it("case F: a CONFIGURING connection is visible, truthful, and not syncable", () => {
    const [panel] = connectionPanels(
      [connection({ status: "PENDING", integration_state: "CONFIGURING" })],
      [],
    );
    expect(panel.presentation).toBe("configuring");
    expect(panel.canSync).toBe(false);
    expect(panel.statusLabel).toBe("Connection setup incomplete");
  });

  it("a remotely removed or re-auth state stays visible but is not syncable", () => {
    const [removed] = connectionPanels([connection({ operational_health: "REMOTE_REMOVED" })], []);
    expect(removed.canSync).toBe(false);
    expect(removed.statusLabel).toBe("Connection unavailable");
    const [reauth] = connectionPanels([connection({ integration_state: "REAUTH_REQUIRED" })], []);
    expect(reauth.presentation).toBe("unavailable");
    expect(reauth.canSync).toBe(false);
  });

  it("a degraded but ACTIVE connection can still sync and says so", () => {
    const [panel] = connectionPanels(
      [connection({ operational_health: "SYNC_DEGRADED", last_sync_failure_category: "x" })],
      [],
    );
    expect(panel.canSync).toBe(true);
    expect(panel.statusLabel).toBe("Connected · last synchronization needs attention");
    expect(JSON.stringify(panel)).not.toContain("secret");
  });
});
