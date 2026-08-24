# Ring device inventory

## Contract and boundary

Stage 1C uses Ring's official Partner API reference and Notifications documentation as reviewed on
2026-08-23. Discovery is the read-only request
`GET /v1/devices?include=status,capabilities,location,configurations`. No unofficial SDK or
reverse-engineered endpoint is used, and this stage performs no media, Event History, configuration,
or streaming operation.

The provider boundary validates a typed, forward-compatible JSON:API document. Fields VeoTrex
depends on are bounded and typed; unknown fields and included resource types are ignored. Compound
relationships resolve only by the exact `(type, id)` linkage, never by ordering or substring.
Missing optional related resources are safe. Malformed linkage and conflicting duplicate included
resources fail the synchronization.

The current device-discovery reference does not advertise pagination (unlike Ring history and
subscription APIs). The client nevertheless follows a returned JSON:API `links.next` defensively:
only the configured Ring API origin is allowed, links cannot repeat, and page/device totals are
bounded. This prevents a future compatible pagination addition from becoming an SSRF or memory
growth path.

## Identity and reconciliation

Ring device and component IDs are exact opaque strings. They are never parsed, truncated, converted
to numbers, used to infer hardware/account identity, or returned as VeoTrex's public camera identity.

The relationship is:

```text
CameraProviderConnection (one signed Ring account)
  -> CameraProviderDevice (one opaque Ring device ID)
       -> CameraProviderComponent (single lens or opaque Ring component)
            -> Camera (stable VeoTrex UUID and later Zone assignment)
```

A device without Ring's components capability receives the private `__single__` reconciliation key
and one Camera. A multi-camera device receives one component and Camera per exact opaque component
ID. This key is internal; the provider component ID remains separately retained.

Sync has a network phase with no database lock followed by a transaction that locks the connection,
rechecks ACTIVE/not-remote-removed state, and upserts device/component identity. A known component
preserves its Camera UUID across provider name changes and removal/reappearance. A full listing never
overrides a stronger signed-removal tombstone; a later signed `device_added` detail reconciliation
restores the historical Camera to `DISCOVERED`, or `ACTIVE` only if it already has an explicit Zone. A new
device is never assigned from its Ring name or location.

A device omitted from a discovery result is left active with its prior `last_seen_at`; omission is
not strong removal evidence. An explicit signed `device_removed` event marks device/components
`REMOVED`, timestamps the loss, and disables Cameras without deleting history. Components explicitly
missing from a present device's current capability document are likewise disabled. This conservative
difference policy avoids turning a partial/transient listing into permanent access loss.

## Normalized state, capabilities, and privacy

`provider_online` plus `status_observed_at` represents only Ring connectivity. It is not stream,
edge-agent, or AI health. Out-of-order online/offline events update it only when their provider time
is at least as new as the stored observation.

Only returned capabilities map into the provider-neutral `CameraCapability` vocabulary. Current
mapping covers live video, receive/send audio, snapshots, and motion when those named capabilities
are present. Codecs/resolutions can remain in minimized component capability details; absent features
are never inferred. Historical clips are intentionally outside Stage 1C.

Configuration is read-only and minimized to deterministic hashes plus booleans indicating configured
privacy and motion zones. Privacy geometry and full Ring payloads are not retained. Ring privacy
controls remain authoritative constraints for later media stages. For multi-camera devices, bounded
`component_id` configuration reads keep each lens's privacy/motion flags distinct; the device-level
configuration is not copied from the first component to every view. Location retains only country code
and region/state as non-authoritative provider metadata; VeoTrex Facility jurisdiction remains the
policy authority and addresses are discarded.

Connection operational health is separate from Stage 1B token lifecycle:

- `ACTIVE`: last inventory operation succeeded.
- `AUTH_DEGRADED`: credential use failed without redefining token lifecycle.
- `REAUTH_REQUIRED`: operator re-link is required.
- `SYNC_DEGRADED`: a non-auth inventory operation failed.
- `REMOTE_REMOVED`: Ring signed that the App Integration was removed; all provider use is disabled.

## Rate limiting and recovery

Ring's documented default is approximately 100 requests/second per partner `client_id`, not per
tenant. A shared in-process client gate prevents tenant-local assumptions about owning the quota.
Safe GETs retry network, 429, and 5xx failures only, with bounded exponential jitter and a maximum of
three attempts by default. Numeric `Retry-After` is honored within a configured bound. 401, 403, 404,
other 4xx, malformed documents, unsafe pagination, and limit violations do not use the generic retry
path. `X-RateLimit-Limit` and `X-RateLimit-Remaining` are treated as optional operational hints, never
trusted input or user-visible data.

This process-local gate is not a distributed rate limiter. A multi-replica production deployment
must add shared quota coordination before scaling synchronization throughput; Stage 1C does not add
Redis solely for that purpose. Recommended low-cardinality metrics are
`ring_device_sync_total`, `ring_device_sync_failures_total`, `ring_devices_discovered`, and
`ring_rate_limited_total`, with no tenant/account/device/camera labels. The repository has no metrics
backend yet, so these are documented rather than coupled to a new monitoring dependency.

Production Ring onboarding remains fail-closed until the Stage 1B managed vault adapter is selected
and installed. Automated tests use mock transports and explicit fixture credentials. No automated
or manual command reads developer-machine Ring secrets, and no live Ring smoke test is run by the
quality gates.
