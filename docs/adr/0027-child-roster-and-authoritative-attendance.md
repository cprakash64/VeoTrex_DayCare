# ADR 0027: Child roster and authoritative attendance check-in/out

- Status: Accepted (V1-04D). Third connected presence source. No alerting.
- Date: 2026-09-26

## Context

After ADR 0026 a classroom's qualified-staff count can come from the operator check-in roster,
but the child count is still a number an operator types into a short-lived manual report. The
engine vocabulary has always contained `PresenceSource.ATTENDANCE` (children, visitors), but
nothing supplied it.

This stage connects ATTENDANCE for children, with a real non-biometric workflow: a facility
roster of children and operator check-in/out into classrooms. It is a control-plane stage. It is
**not** a child computer-vision stage, and it does **not** implement parent or guardian
association.

## Decision

### A child is a roster entry, not an identity

`child_profiles` is facility-scoped (a child attends one facility) and holds exactly:

| Column | Why |
| --- | --- |
| `id`, `tenant_id`, `facility_id` | opaque identity and scope |
| `display_name` (1–120) | so authorised operators can recognise the entry on their own screens |
| `status` ACTIVE / INACTIVE / ARCHIVED | lifecycle |
| `external_reference` (optional, ≤ 64) | a key for a future attendance-system connector |
| `created_by_actor_id`, `created_at`, `updated_at` | provenance |

**Data minimisation.** There is deliberately no photo, face, face crop, embedding, biometric
template, date of birth, age, home address, medical/allergy, guardian, pickup-authorisation,
camera or track column. The display name is ordinary roster PII that an operator enters; it is
not identity evidence and nothing derives it from, or attaches it to, imagery. A date of birth or
photograph would add risk for no ratio benefit: the configured policy's age band belongs to the
classroom (ADR 0024), not to the child.

Protection of the display name:

- NFC-normalised, whitespace-collapsed, 1–120 characters; control characters, invisible
  formatting characters (zero-width, bidi overrides), surrogates, private-use and unassigned code
  points, and `<` / `>` are refused — by the API, by the web form, and by a database CHECK
  (length, `[[:cntrl:]<>]`, no surrounding whitespace).
- Rendered as text only (React escaping; no `dangerouslySetInnerHTML`).
- Never in audit metadata, never in logs, never handed to the count resolver or the ratio
  engine, never on any `/v1/edge` or Ring surface, never on the edge dashboard.
- Returned only by the authenticated child-roster and attendance endpoints.

`external_reference` accepts identifier characters only (`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$`,
so it cannot hold a sentence or a name), is unique per facility when present (partial unique
index), and is never a biometric identifier.

Lifecycle: ACTIVE ↔ INACTIVE freely; either → ARCHIVED, which is terminal (no edits, no
reactivation). Only ACTIVE children can be checked in or refreshed. Nothing is deleted: the
runtime role has SELECT, INSERT, UPDATE and no DELETE. A child checked in when deactivated or
archived stops counting at once and can still be checked out.

### Append-only attendance events

`child_attendance_events` mirrors ADR 0026's staff events: tenant, facility, classroom
(`area_id`), child, per-child `sequence`, `CHECKED_IN` / `REFRESHED` / `CHECKED_OUT`, source
`ATTENDANCE` (CHECK), server-clock `occurred_at`, `valid_until` (open events only),
`checked_in_at` (start of the stay), recording actor, `created_at`. No image, face, embedding,
track, camera, parent or guardian column exists.

The runtime role has **SELECT and INSERT only**. Current attendance is derived from each child's
latest event; there is no mutable "is present" flag.

Composite FKs to `child_profiles (id, facility_id, tenant_id)` and `areas (id, facility_id,
tenant_id)` make a child in another facility's classroom, or another tenant's, unstorable.

### One current classroom; moves; concurrency

A child's latest event names at most one classroom, so two current classrooms are
unrepresentable. Transitions (pure `plan_check_in` / `plan_refresh` / `plan_check_out`):

- not checked in, checked out, or lapsed here → `CHECKED_IN`;
- present here → 409 `child_already_checked_in` (use refresh; a repeated click never silently
  resets the lease);
- open (present or lapsed) in another classroom → **atomic move**: `CHECKED_OUT` there and
  `CHECKED_IN` here, same transaction, same timestamp, audited as `attendance.moved`;
- check-out here → `CHECKED_OUT` (also closes a lapsed stay); present elsewhere → 409
  `child_in_another_classroom`; otherwise an idempotent 200 that appends nothing;
- refresh → only while PRESENT here; lapsed → 409 `attendance_expired` (check in again).

`UNIQUE (tenant_id, child_profile_id, sequence)` is the guarantee: writers that read the same
state append the same position and only one commits (409 `attendance_state_changed`). A
per-child transaction-scoped advisory lock turns that race into a wait, and state and clock are
read after the lock. A DB-backed test fires ten simultaneous check-ins into two rooms and
asserts a gap-free stream and exactly one current room.

### Freshness and expiry

Lease **30 minutes – 12 hours, default 12 hours**, DB-constrained, explicit in every attendance
response (`lease_min_seconds`, `lease_max_seconds`, `lease_default_seconds`).

A daycare session legitimately lasts a working day, so the lease is long — but the maximum is
the ratio engine's own outer bound on any count (`MAX_VALIDITY_SECONDS` = 12 h, ADR 0024), not the
18 h first suggested: no count in the system is trusted beyond 12 hours, and a forgotten
check-out therefore lapses within 12 hours, never carrying yesterday's attendance into today's
session. Expiry is a UTC instant; facility time is presentation only. Refresh extends a fresh
stay from now; an expired stay needs a new check-in.

### The authoritative child count

`resolve_child_count(classroom, facility, now, members, events)` is pure. A child counts only
when the profile exists, belongs to the classroom's facility, is ACTIVE, and their latest event
is an unexpired check-in to this classroom. `members` carry an id, a facility and a status —
never a name. The result is aggregate: `count`, `present`, `present_inactive`, `stale`,
`valid_until` (earliest counted lease), `source = ATTENDANCE`, `freshness`, `evaluated_at`.
There is no parameter through which a camera, a person track, a recognition result or an UNKNOWN
person could add a child. (No "ambiguous" bucket exists: a child has one facility and one stream,
so no conflicting state can be stored.)

### Source mode and precedence

`areas.presence_source_mode` gains `ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF`:

| Mode | CHILD | QUALIFIED_STAFF | VISITOR |
| --- | --- | --- | --- |
| `MANUAL_AGGREGATE` | MANUAL | MANUAL | MANUAL |
| `ROSTER_STAFF_PLUS_MANUAL_CHILDREN` | MANUAL | STAFF_ROSTER | MANUAL |
| `ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF` | ATTENDANCE | STAFF_ROSTER | MANUAL when fresh |

- `AUTHORITATIVE_SOURCES[CHILD]` = {MANUAL, ATTENDANCE}; ATTENDANCE never decides staff or
  visitors. STAFF_RECOGNITION and vision are never authoritative.
- Sources are never combined within a slot. In attendance mode a manual report may carry
  visitors only: a child number is refused (409 `child_count_comes_from_attendance`), a staff
  number is refused (409 `staff_count_comes_from_roster`), and `visitor_count` must be stated
  (422 otherwise). `classroom_presence_snapshots.child_count` became nullable to store that.
- An older manual report's child or staff number is ignored in attendance mode, never added, and
  not shown in the `presence` block.
- Switching is explicit and audited (`classroom.presence_source_mode_changed`). Switching back to
  roster or manual mode never copies the attendance count: the child slot reads
  `CHILD_COUNT_MISSING` until an operator reports one. V1-04B history stays immutable.

### Visitors

The configured ratio uses children and qualified staff only; visitors never block it. In
attendance mode the latest manual report supplies visitors when fresh. A missing or stale visitor
value stays `MISSING` / `STALE` with `count: null` — never zero — and reconciliation (already
built this way in ADR 0024) reports `visitors_included: false` with
`VISITOR_COUNT_NOT_SUPPLIED` instead of pretending. Only an operator's explicit 0 is zero.

### Ratio engine integration

Unchanged formula and engine. `compose_presence` builds CHILD from `resolve_attendance`,
QUALIFIED_STAFF from `resolve_roster`, VISITOR from the manual report, then `evaluate_ratio`.
Both derived counts are fresh at evaluation, so attendance mode never reads INSUFFICIENT_DATA for
presence reasons: 0 children is `NO_CHILDREN_PRESENT`, 0 staff with children is
`OVER_CONFIGURED_RATIO`. `ratio-status.sources` gains `child_attendance`; `presence_connected`
is true in attendance mode.

### Vision and face-recognition prohibition

No camera, occupancy count, person track, UNKNOWN person or face-recognition result can create,
refresh, move or end attendance, or contribute to the child count. No child face recognition,
child embedding, child template, child face crop or child image exists or is planned. Proved by
`test_child_identity_boundary.py`: no child columns of those kinds; child modules import no
face/Ring/edge/camera code and vice versa (AST); no child route under `/v1/edge` or
`/v1/integrations`; no child face/photo/upload route; attendance requests accept only a child
UUID and a lease; an edge-style machine token cannot read the roster; a real evaluation-route
face MATCH and UNKNOWN write zero attendance events.

### Edge isolation

Nothing child-related reaches the Jetson. The edge authenticates only for the WHEP broker
(ADR 0021); child routes are human-only (Auth0). The edge-agent source contains no child or
attendance identifier (tested), and the live dashboard's ban on demographic words is unchanged.

### API

| Method | Path | Permission |
| --- | --- | --- |
| GET / POST | `/v1/facilities/{facility_id}/children` | read:operational / administer:facility |
| GET / PATCH | `/v1/children/{child_id}` | read:operational / administer:facility |
| POST | `/v1/children/{child_id}/activate`, `/deactivate`, `/archive` | administer:facility |
| GET | `/v1/classrooms/{classroom_id}/attendance` (last 20 events) | read:operational |
| POST | `…/attendance/check-in` (201), `…/refresh`, `…/check-out` | administer:facility |

Unknown, other-tenant, other-facility and unreadable ids are a uniform 404 (`not found`), with
the same code path for each; readable but not administrable is 403; validation 422; state
conflicts 409 with a bounded category. Error bodies never echo input.

### Audit

`audit_events`, ids and state transitions only:

- `child.created` (facility, status, `external_reference_present`), `child.updated`
  (`changed_fields` names only), `child.activated` / `.deactivated` / `.archived`
  (from/to status);
- `attendance.checked_in`, `.moved` (`from_classroom_id`), `.refreshed`, `.checked_out`
  (classroom, facility, child id, event ids, previous state, lease and `valid_until`).

Never a display name or external reference (tested), never an image, face or track.

### Web

A Child rosters page (per facility: add, rename, set/clear reference, deactivate, reactivate,
archive), a Child attendance card on the classroom page (check in, move here, refresh, check out,
a lease choice, per-second expiry, bounded recent events), a three-way Presence sources control,
a visitors-only manual form in attendance mode that states "Children: Attendance · Qualified
staff: Staff roster · Visitors: Manual", and ratio-card provenance ("Children present: 6 —
Attendance"). No photo, face, biometric, date-of-birth or guardian UI; no legal-compliance
wording.

### Future boundaries (not implemented)

- **Parent / guardian association and pickup authorisation are NOT implemented in this stage.**
  They need their own ADR covering consent, verification, retention and access. (Implemented
  without biometrics in V1-04E - see ADR 0028.)
- **External attendance connector**: a future integration may key on `external_reference` to
  write ATTENDANCE events through the same pure transitions and the same append-only table, with
  its own actor identity and freshness semantics; it must never infer attendance from cameras.

## Consequences

- Once a classroom is in attendance mode, nobody types a child count; it follows check-ins.
- Operators must check children out (or accept expiry within 12 hours) and refresh long stays.
- `child_attendance_events` grows with every action; retention/archival is a later stage.
- The migration 0012 downgrade refuses while any child, attendance event, visitor-only report or
  attendance-mode classroom exists, rather than destroying or inventing data.
- No alerting, no guardian data, no child vision, no jurisdictional ratios, no legal claim.
