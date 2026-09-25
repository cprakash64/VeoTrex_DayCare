# ADR 0021: Edge machine authentication and brokered Ring WHEP

- Status: Accepted (V1-DEMO-03B)
- Date: 2026-09-24

## Context

The Jetson's WebRTC/WHEP stack (ADR 0014) can negotiate a Ring live session, but it needs a
Ring OAuth bearer to do so, and Ring OAuth tokens live only in the control plane's credential
vault (ADR 0007, ADR 0015). An EdgeNode had no identity of its own toward the control plane:
`edge_nodes` and `camera_assignments` existed, but no credential and no API used them.

## Decision

```
Edge Node
   |
   | machine credential + VeoTrex camera UUID + SDP offer
   v
VeoTrex API   POST /v1/edge/cameras/{camera_id}/whep
   |
   | 1. authenticate the machine (SECURITY DEFINER, server-resolved tenant)
   | 2. authorize: ACTIVE assignment of this camera to this node, under RLS
   | 3. Ring access token from RingLinkService (vault, refresh, rotation)
   v
Ring WHEP     POST {api}/v1/devices/{device}/media/streaming/whep/sessions
   |
   | SDP answer + Ring session Location
   v
VeoTrex API   keeps the Location in a process-local lease
   |
   | 201 Created: SDP answer + Location: /v1/edge/whep-leases/{opaque}
   v
Edge Node     DELETE /v1/edge/whep-leases/{opaque}  ->  API DELETEs the Ring session
```

### Ring OAuth stays server-side

The edge receives an SDP answer and an opaque lease, nothing else. The control plane already
holds the only copy of each tenant's Ring refresh token and is the single authority for refresh
and rotation (`RingLinkService.get_valid_access_token`). Handing even an access token to a
device on a daycare LAN would multiply where a Ring account credential can be stolen from, and
make revocation depend on a device we do not physically control. The broker calls Ring with
the same client, proxy routing and bounded error taxonomy as every other Ring API call. A WHEP
POST that failed ambiguously (transport error, 5xx) may have created a session and is never
replayed; only a definite 401 earns one lifecycle-controlled forced refresh and one retry.

### Machine identity is separate from human identity

An EdgeNode is not a person and has no Auth0 organization, subject or role grants. It holds a
dedicated credential, `vte1.<selector uuid>.<256 random bits>`, issued once by the admin-only
console script `veotrex-edge-credential` into a new 0600 file (never printed; stdout, `/dev`,
`/proc`, existing paths and symlinks refused) and revocable without deleting history. Only a
SHA-256 digest bound to the selector is stored, in `edge_node_credentials` (forced RLS). The API
runtime role has no privilege on that table; it executes one SECURITY DEFINER function that looks
up one credential by primary key and returns `(tenant_id, edge_node_id, facility_id)` only when
the credential is ACTIVE, the digest matches, the node is not DISABLED and its tenant and
facility are ACTIVE. That result - never a caller-supplied value - becomes `app.tenant_id`.
Every failure is the same 401. An edge credential is refused by the human routes before it
reaches the JWT verifier, and a human token cannot pass the edge credential's strict format, so
neither credential opens the other's surface. `edge_nodes` and `camera_assignments` are now
readable (SELECT only, under RLS) by the runtime role; neither is writable by it.

### Camera assignment is the authorization

The edge names a VeoTrex camera UUID and never a Ring device or component id. The broker
resolves the Ring identity only when every condition holds: an assignment of exactly this camera
to exactly this node with `ended_at IS NULL`; the camera not DISABLED/ARCHIVED; its Ring
component and device ACTIVE; its connection RING, ACTIVE and not remotely removed or awaiting
re-authorization; and a LIVE_VIDEO capability. Another tenant's camera is invisible under RLS,
and a nonexistent, unassigned, reassigned, ended, disabled or capability-less camera all return
the same 404, so the edge cannot probe inventory. Assignment is what lets an operator move a
camera between nodes, or take it away, without touching any credential.

### The provider session hides behind an opaque lease

Ring's session Location identifies a live video session of a customer's camera and is the handle
that tears it down. The edge gets `Location: /v1/edge/whep-leases/<43 random characters>`
instead. The broker keeps the Ring URL, validates it against the configured Ring origin before
ever sending a bearer to it, and DELETEs it when the owning node DELETEs the lease, when the
lease expires, and at shutdown. A lease of another node is indistinguishable from none; a
repeated DELETE by the owner is idempotent.

### The lease registry is process-local, so the API runs one worker

For this single-control-plane demo the registry is in memory: bounded globally
(`VEOTREX_EDGE_WHEP_MAX_ACTIVE_LEASES`, default 16) and per node (default 4), bounded in age
(`VEOTREX_EDGE_WHEP_LEASE_TTL_SECONDS`, default 3600), lock-protected, swept by a background
task, and drained at shutdown before the Ring client closes. That is only correct while one
process serves every request, because a DELETE routed to a process that never issued the lease
cannot release the Ring session. The API image therefore runs `uvicorn --workers 1`
(previously 2), and a test pins it. Raising the worker count, or running replicas, requires a
shared lease store first.

## Consequences

- The Jetson's existing `WhepClient` consumes the broker unchanged in shape; `BrokerWhepClient`
  only changes origin, session path and Location rule. `BrokeredWhepSessionProvider` reads the
  credential from a protected file per session, and `BrokeredWhepExchange` is the
  `AnswerExchange` for `WebRtcMediaBackend`. The live CLI still refuses `--source ring`.
- One API worker halves request concurrency on the VPS; the API is async and the face backend is
  `unavailable` there, so this is acceptable for the demo but is a scaling limit.
- Lease expiry ends a live session after the TTL even if it is healthy; the edge reconnects under
  its bounded reconnect budget. Ring documents no session lifetime, so none is invented.
- Every opened session writes an `edge.whep.session_opened` audit row; a session whose audit
  write fails is torn down rather than granted.
- The runtime-role probe grows from 21 to 23 checks.
