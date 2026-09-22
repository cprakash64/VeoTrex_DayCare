export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function providerStatus(value: boolean | null): string {
  return value === true ? "Ring online" : value === false ? "Ring offline" : "Ring status unknown";
}

/**
 * What the API tells the dashboard about one Ring connection. Lifecycle and sync state
 * only: no credential reference, no provider account identifier (V1-01A-1).
 */
export type RingConnectionSummary = Readonly<{
  connection_id: string;
  display_name: string;
  status: string;
  integration_state: string;
  operational_health: string;
  last_synchronized_at: string | null;
  last_sync_failure_category: string | null;
}>;

export type RingCameraSummary = Readonly<{ camera_id: string; connection_id: string }>;

export type ConnectionPresentation = "active" | "configuring" | "disconnected" | "unavailable";

export type ConnectionPanel<Camera extends RingCameraSummary> = Readonly<{
  connectionId: string;
  displayName: string;
  presentation: ConnectionPresentation;
  /** Operator wording for the lifecycle state; never a raw provider or credential detail. */
  statusLabel: string;
  /** True only for an ACTIVE, not remotely removed connection: the sync control may render. */
  canSync: boolean;
  /** True when the connection has never completed an inventory sync. */
  neverSynchronized: boolean;
  cameras: ReadonlyArray<Camera>;
}>;

/**
 * Join connections with their cameras, connection-first. A connection with zero cameras is a
 * first-class panel, so a freshly linked account renders its own sync control instead of
 * disappearing behind "no cameras" (the V1-01A-0 finding). Cameras whose connection is not
 * listed are ignored rather than inventing a connection for them. Order is deterministic.
 */
export function connectionPanels<Camera extends RingCameraSummary>(
  connections: ReadonlyArray<RingConnectionSummary>,
  cameras: ReadonlyArray<Camera>,
): ReadonlyArray<ConnectionPanel<Camera>> {
  const byConnection = new Map<string, Camera[]>();
  for (const camera of cameras) {
    const list = byConnection.get(camera.connection_id) ?? [];
    list.push(camera);
    byConnection.set(camera.connection_id, list);
  }
  return [...connections]
    .sort(
      (left, right) =>
        left.display_name.localeCompare(right.display_name) ||
        left.connection_id.localeCompare(right.connection_id),
    )
    .map((connection) => {
      const presentation = presentationOf(connection);
      return {
        connectionId: connection.connection_id,
        displayName: connection.display_name,
        presentation,
        statusLabel: statusLabelOf(connection, presentation),
        canSync: presentation === "active",
        neverSynchronized: connection.last_synchronized_at === null,
        cameras: byConnection.get(connection.connection_id) ?? [],
      };
    });
}

function presentationOf(connection: RingConnectionSummary): ConnectionPresentation {
  if (connection.integration_state === "DISCONNECTED" || connection.integration_state === "ARCHIVED") {
    return "disconnected";
  }
  if (connection.integration_state === "CONFIGURING") return "configuring";
  if (connection.integration_state === "ACTIVE" && connection.operational_health !== "REMOTE_REMOVED") {
    return "active";
  }
  return "unavailable";
}

function statusLabelOf(
  connection: RingConnectionSummary,
  presentation: ConnectionPresentation,
): string {
  switch (presentation) {
    case "active":
      return connection.operational_health === "ACTIVE"
        ? "Connected"
        : "Connected · last synchronization needs attention";
    case "configuring":
      return "Connection setup incomplete";
    case "disconnected":
      return "Disconnected";
    default:
      return "Connection unavailable";
  }
}
