# ADR 0030: Edge portal configuration distribution and room-event persistence

- Status: Accepted (V1-05B). Implemented and tested synthetically; **not deployed**. No edge
  node consumes the new routes yet, so `edge_distribution` stays `NOT_CONNECTED`.
- Date: 2026-09-26
- Builds on: ADR 0029 (portal crossing), ADR 0021 / V1-DEMO-03B (edge machine credential).

## Context

V1-05A made anonymous room entry/exit a qualified edge fact, but left two gaps (ADR 0029,
"Future boundaries"): doorway lines drawn in the web app never reached the edge (operators copied
`--portal` flags by hand), and events lived only in the edge session. V1-05B closes both, without
widening what an edge node can see and without adding identity.

**This stage does NOT identify teachers, children, guardians or anyone else.** An event says a
person crossed a doorway line - nothing about who.

## Decision

### The machine credential stays narrow

Both new routes use the existing `vte1` edge credential (ADR 0021, migration 0008) and the same
`EdgePrincipalDependency`. There is no second authentication mechanism, no new grant to a human
route and no widening of the credential. Node, tenant and facility come **only** from the
credential (`authenticate_edge_node_credential`, SECURITY DEFINER); no query parameter, header or
body field can name another node, tenant, facility or classroom (unknown body fields are 422).
The credential cannot open any human route (children, staff, guardians, attendance, portals,
the room-transition timeline): those use the Auth0 verifier and answer 401.

### `GET /v1/edge/runtime-config`

Returns, for the authenticated node only, each camera with an **active** assignment to that node
(assignment not ended, node not DISABLED, camera DISCOVERED/ACTIVE; at most 32), and for each the
ACTIVE portals of the classroom the camera is currently in:

```json
{"schema_version": 1, "edge_node_id": "…", "config_version": "sha256:…",
 "cameras": [{"camera_id": "…", "assignment_id": "…", "configuration_revision": "sha256:…",
   "portals": [{"portal_id": "…", "label": "Main Door", "x1": 0.5, "y1": 0.05, "x2": 0.5,
     "y2": 0.95, "inside": "RIGHT", "enabled": true, "deadband": 0.02, "revision": 1}]}]}
```

Nothing else: no provider device id, Ring token or account, camera or classroom name, staff,
children, guardians, attendance, policy, face data or templates. A camera whose classroom lies in
another facility than the node's own keeps its place with **no** portals; a portal drawn for the
camera's previous classroom is not sent. `Cache-Control: no-store`; rate-limited.

### Config versioning

`configuration_revision` = `sha256:` + SHA-256 of the canonical JSON (sorted keys, no whitespace,
ASCII, no NaN) of `{camera_id, assignment_id, portals}` with portals sorted by `portal_id`.
`config_version` hashes the sorted `(camera_id, revision)` pairs. Identical configuration gives
the identical revision; any coordinate, label, enabled/disabled, dead-band or side edit, archive
or removal, or re-assignment gives a new one; portal order cannot change it. The edge recomputes
both hashes with byte-identical code and refuses a document whose hashes do not match.

### Polling and last-known-good (edge)

`--managed-portals` runs one daemon **refresher** thread: first attempt immediately (so startup
never waits on the network), then every `--config-refresh-seconds` (default 60, bounded 30-600).
Each request is HTTPS only, no redirects (3xx refused, never followed), a 10 s socket timeout plus
a 10 s total read budget, at most 256 KiB, JSON only, and re-reads the 0600 credential file.
A document is validated **whole** (types, bounds, counts, the same `Portal` rules as `--portal`,
both hashes); one bad field and nothing changes. A valid document for this camera is staged and
applied by the pipeline thread between observations, atomically, dropping all crossing state
(nothing synthesised). A transient or malformed refresh keeps the last-known-good and counts the
failure. The last valid document is cached (geometry only, 0600 file in a 0700 directory owned by
the user, atomic replace, rewritten only when it changes) and used at the next start until the
first successful fetch. A missing, unreadable, symlinked, too-permissive, oversized, unparseable
or hash-mismatched cache is ignored: **fails closed**, no portals until the control plane answers.
If the camera is no longer assigned to the node, its portals become empty.

### Local vs managed precedence (CLI)

| Environment | `--portal` | `--managed-portals` | both |
|---|---|---|---|
| local / development / test / ci | static lines, events stay local | control-plane lines, events uploaded | refused |
| staging / production / anything else | **refused** | the only source | refused |

All validation (camera UUID, HTTPS origin, credential file, absolute private `--state-dir`,
refresh interval, outbox capacity) happens before any camera or GPU work. No hard-coded
coordinates can silently override the control plane.

### Anonymous event schema and idempotency

`POST /v1/edge/events/room-transitions` takes 1-100 events per batch (body at most 64 KiB,
refused before authentication):

`event_id` (UUIDv4, generated on the edge **before** the first upload), `camera_id`, `portal_id`,
`event_type` (`PERSON_ENTERED_ROOM` / `PERSON_EXITED_ROOM`), `occurred_at` (timezone-aware),
`ephemeral_track_id` (1..2^31-1), `stream_instance_id` (a random per-run id), `crossing_x/y`
(finite, 0..1), `evidence_observations` (1..100).

**Ephemeral track ids are not identity.** A track id is a number the tracker reused within one
camera session; it is meaningful only together with `stream_instance_id`, is never joined to any
roster and is never shown to operators.

For each event the server checks, from its own rows: the camera is actively assigned to this
node (and placed in the node's facility); the portal belongs to that camera and to the classroom
the camera is in; `occurred_at` is at most 120 s ahead of and 7 days behind receipt. Tenant,
facility and classroom are then taken from those rows. Results are per event: `ACCEPTED`,
`DUPLICATE` (same id, same content - a retry) or `REJECTED` with a bounded category
(`camera_unavailable`, `portal_unavailable`, `occurred_at_out_of_window`, `event_id_conflict`,
`invalid_event`). "Not yours" and "does not exist" are indistinguishable. The primary key on
`event_id` makes any retry exactly one row.

### Durable outbox, retry and dead-letter (edge)

The runtime hands each transition to the session sink; the payload (exactly the fields above) is
written to a SQLite outbox (WAL, `synchronous=FULL`, 0600 in 0700, WAL bounded to 1 MiB) before any
upload; re-enqueuing an id already queued is a no-op. Capacity default 10 000 events (≤ 512 bytes
each), hard maximum 100 000; when full a new event is refused and counted
(`room_transition_queue_dropped_total`) - the detector never blocks. An uploader thread sends the
oldest first in batches of 50:

- ACCEPTED / DUPLICATE → removed.
- Transport failure, 5xx, 429, 404/405/408 → same ids retried with exponential backoff 2 s → 5 min
  plus bounded jitter.
- 401/403, missing credential, refused redirect → wait the maximum, counted, never discarded.
- 400/413/422 on a batch → retried one event at a time; the single refused event, or any
  per-event REJECTED, moves to a bounded dead-letter table (1 000 rows, oldest evicted and counted).

Events survive restarts and outages; the same `event_id` is used on every attempt.

### Storage and retention boundary

`room_transition_events` (migration 0015): append-only (runtime SELECT/INSERT only), forced RLS,
PUBLIC revoked, composite tenant-scoped FKs to facility, classroom (area), camera, node and portal
(RESTRICT), CHECKs for type, crossing range (NaN/Infinity fail), evidence, track range, stream id
shape and the occurred-at window. No name, image, frame, crop, face or embedding column. Indexes
serve the timeline and a future retention job (`tenant_id, received_at`). **No retention job
exists yet**; rows accumulate until one is approved. The downgrade refuses while any event exists.

### Operator timeline

`GET /v1/classrooms/{id}/room-transitions` (read:operational, classroom-scoped, uniform 404 for
unknown / other-tenant / unreadable classrooms): newest first, keyset cursor, `limit` 1-200
(default 50), optional camera / portal / type filters. Rows carry event id, type, times, camera
and portal (id and label) - not the track or stream id. The web page *Room transitions* shows one
bounded page at a time as "Person entered via Main Door" / "Person exited via Main Door", with
loading, empty and error states. No teacher, child or guardian wording and no identity inference;
an entry is never paired with an exit.

### Metrics (edge, in the dashboard state and exit report)

Config: fetch success/failure/rejected totals, changes, cache write failures, last failure
category, source (cache/control_plane), config version, camera revision, config age. Events:
generated, queued, duplicate enqueue, uploaded, duplicate ACK, rejected, dropped, dead-letter
evicted, upload failures, auth failures, queue depth/capacity, dead-letter depth/capacity.
Counters and depths only - no per-track labels.

### Validation errors

The API's 422 body no longer echoes rejected input (FastAPI's default could not serialise a JSON
NaN and turned it into a server error). Same `detail` list shape: `type`, `loc`, `msg`.

## Not done / deployment state

- **Not deployed.** No migration applied to staging/production, no node runs `--managed-portals`,
  `veotrex-edge.service` unchanged. `edge_distribution` therefore remains `NOT_CONNECTED` and the
  UI still says "Not yet sent to cameras". It becomes `CONNECTED` only when a deployed node
  consumes the route - ideally backed by a per-camera "last fetched revision" signal.
- No retention job; no ignore-region distribution; rate limits are process-wide, not per node.
- No real Ring run in this stage (V1-05A already qualified crossings: EXIT, ENTER, second ENTER).

## Consequences

- An operator edit reaches a managed node within one refresh interval once deployed; a broken or
  unreachable control plane never stops video and never replaces good lines with bad ones.
- Room events become durable, idempotent, tenant-scoped records - still anonymous line crossings,
  still not a head count, never an input to ratios.
