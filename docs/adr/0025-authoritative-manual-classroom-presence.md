# ADR 0025: Authoritative manual classroom presence

- Status: Accepted (V1-04B). First connected presence source. No alerting.
- Date: 2026-09-25

## Context

ADR 0024 built configured classroom ratio policies and a pure ratio engine that takes role counts
only from approved presence sources, never from a camera. No source was connected yet, so every
classroom honestly read `INSUFFICIENT_DATA`, shown as "Presence counts not connected".

## Decision

### Manual aggregate counts come first

An authorized operator (`administer:facility` on the classroom's facility) reports three numbers
for a room:

- children present;
- qualified staff present;
- visitors present.

The report is labelled `MANUAL` (operator-reported). It is the smallest input that makes a
configured ratio useful, and it requires no identity at all. It stores no names, no child or
staff identifiers, no images, faces, embeddings, tracks or boxes.

The API accepts exactly `child_count`, `qualified_staff_count`, `visitor_count` and
`valid_for_seconds`. Anything else is refused with 422: a timestamp, an UNKNOWN count, names.

Bounds are chosen to catch typos, not to encode any regulation:

- 0 to 150 children (a combined multipurpose room);
- 0 to 50 qualified staff;
- 0 to 50 visitors.

### Append-only history

`classroom_presence_snapshots` (migration 0010) stores one row per report:

- tenant, facility and classroom;
- the three counts and the source (`MANUAL`, enforced by CHECK);
- `observed_at` and `valid_until` in UTC;
- the submitting actor and `created_at`;
- `revoked_at` and `revoked_by_actor_id`.

The database enforces the rules itself:

- **CHECKs** cover the count bounds, a validity of 30 s to 15 min, `observed_at` no more than
  120 s after `created_at`, the revocation fields being set together, and revocation coming
  after creation.
- A **composite FK** to `areas (id, facility_id, tenant_id)` (a new unique key) makes a
  classroom/facility mismatch unstorable.
- **Forced RLS** with `tenant_isolation`, and `REVOKE ALL FROM PUBLIC`.
- **Runtime grants:** SELECT, INSERT and UPDATE. There is no DELETE.
- **Index** `(tenant_id, area_id, observed_at DESC, created_at DESC, id DESC)` serves "latest
  report".

A new update is a new row; nothing is edited. The `classroom_presence_snapshot_guard` BEFORE
UPDATE trigger allows exactly one change ever: `revoked_at` and `revoked_by_actor_id` going from
NULL to set. Any other UPDATE, and any second revocation, fails in the database, not only in the
API.

The trigger function is SECURITY DEFINER with `search_path = pg_catalog, public`. This is not
used for privilege: the project's function contract (ADR 0018, runtime-role tests section 11)
requires it of every function in `public`. The body runs no SQL and reads no table. No role,
the runtime included, has EXECUTE on it; PostgreSQL invokes triggers without that check.

### Freshness

`observed_at` is the **server's clock at submission**. A client can neither backdate a report
nor send one from the future.

Validity is chosen from 30 s to 15 min, default **120 s**. That is short because a head count
describes a moment; it is shorter than the engine's general 12 h bound, which remains the outer
limit.

Freshness reuses ADR 0024 exactly: fresh while `now < valid_until`; a timestamp more than 120 s
in the future is not trusted. At `now >= valid_until` the report is `PRESENCE_STALE`, the ratio
becomes `INSUFFICIENT_DATA`, and neither the counts nor the previous result are shown.

The web ratio card also re-checks expiry on the browser clock every second and re-reads the
server every 15 s. A page left open never shows an expired result.

### Revocation

`POST …/presence/{id}/revoke` marks one report revoked, recording the actor and time. It is
idempotent: a second revoke changes nothing, keeps the first actor and time, and is not audited
again. Nothing is ever deleted.

### Deterministic selection; supersession is permanent

The authoritative report is the classroom's single **latest** row, ordered by
`observed_at DESC, created_at DESC, id DESC`. The same ordering is used in SQL and in
`ManualPresenceRecord.ordering_key`, and the id breaks exact ties, so identical data always
selects the same row. Two administrators submitting at the same moment both get a row, and the
ordering decides which counts.

Only that latest row is ever considered:

- if it is revoked: `PRESENCE_REVOKED`;
- if it is stale: `PRESENCE_STALE`;
- if it is beyond the skew: `PRESENCE_NOT_YET_VALID`;
- if it is fresh: `PRESENCE_FRESH`;
- if there is none: `PRESENCE_NOT_CONNECTED`.

An earlier report is **never resurrected**. An operator who withdraws or lets lapse the current
count must not find an older one silently authoritative again. History marks older rows
`SUPERSEDED`.

### Ratio engine integration

The flow is: classroom → policy in force → latest report → `resolve_manual_presence` (pure) →
`ManualPresenceRecord.to_snapshot()` → the unchanged `evaluate_ratio`.

`to_snapshot()` fills exactly the CHILD, QUALIFIED_STAFF and VISITOR slots with source MANUAL. It
never creates an UNKNOWN count. The existing slot checks still refuse UNKNOWN, visitor or vision
values in the child or staff slots.

A stale report is passed through as-is, so the engine itself reports it stale; a revoked report
reads as missing. Storage and resolution live in `ClassroomService`; the arithmetic stays in the
engine.

`GET /v1/classrooms/{id}/ratio-status` adds a `presence` block:

- availability, source, snapshot id, `observed_at` and `valid_until`;
- freshness;
- the counts, **only while fresh**.

All timestamps are normalised to UTC in responses, whatever the database session timezone.

### Vision reconciliation boundary

Vision is still not connected; the edge runtime is unchanged. `reconcile_vision` stays a
separate, non-mutating diagnostic.

For example, with manual 6 children, 1 staff and 0 visitors, and 8 people seen, the ratio stays
6 : 1, reconciliation reports `VISION_HIGHER_THAN_ROSTER` with one *unexplained* person, and the
stored report is untouched.

The live edge dashboard keeps "Presence counts not connected". The edge authenticates only with
its machine credential for the WHEP broker (ADR 0021). Reusing that credential to read classroom
state would widen its scope, and a second edge authentication channel is out of scope.

### Audit

The existing `audit_events` are used:

- `presence.manual_submitted`: classroom, source, the three counts and validity;
- `presence.manual_revoked`: classroom and source.

They carry no names, tokens, images or camera data. Runtime access to audit events remains
append-only.

### Source precedence is deferred on purpose

MANUAL is the only connected source, and the latest report wins. Future adapters would plug in as
`PresenceCount` producers:

- ATTENDANCE, for children and visitors;
- STAFF_ROSTER and STAFF_RECOGNITION, for qualified staff only.

Combining sources (which wins, how disagreement is shown, whether a partial source may fill one
slot) is a product and safety decision with real failure modes. It needs its own ADR and must
not be guessed now.

## Consequences

- Ratio status is useful with no identity data at all, and it degrades to `INSUFFICIENT_DATA`
  the moment a count lapses.
- The table grows with reports. Retention and archival are a later operational stage; history
  is bounded only in API responses (last 20).
- There is no alerting, no attendance, no teacher recognition and no multi-camera fusion.
  Nothing claims legal compliance, and no jurisdiction numbers are stored.
