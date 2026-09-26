# ADR 0026: Ratio-eligible staff roster and authoritative staff check-in/out

- Status: Accepted (V1-04C). Second connected presence source. No alerting.
- Date: 2026-09-26

## Context

ADR 0025 connected MANUAL presence: an operator reports children, qualified staff and visitors
as one short-lived aggregate. The staff number in that report is typed by hand, so it cannot say
*who* is in the room, and it cannot tell a teacher from a cook.

V1-02A already holds enrolled adult staff profiles (`staff_profiles`, tenant-wide, status
ACTIVE / INACTIVE / DELETED). Nothing links a profile to a facility or a room, and nothing
records whether an operator considers a person to count toward the configured classroom ratio.

This stage replaces only the qualified-staff input with a staff-specific roster. The child
count stays manual. Face recognition stays out of it entirely.

## Decision

### Three separate facts about a staff member

| Fact | Where | Who decides |
| --- | --- | --- |
| Identity: who the enrolled adult is | `staff_profiles` (V1-02A) | operator with `manage:staff` |
| Eligibility: on a facility's roster, and whether they count toward the configured ratio | `staff_ratio_eligibility` | operator with `administer:facility` there |
| Presence: checked into a classroom, until when | `staff_presence_events` | operator with `administer:facility` there |

A face-recognition result is a fourth thing, a *possible observation*. It is none of the above
and writes to none of them (see "Recognition boundary").

### Operator-designated ratio eligibility, not a qualification

`counts_toward_ratio` means exactly: *an operator designated this person as counting toward the
configured classroom policy at this facility*. VeoTrex verifies no licence, certification,
training or legal status and stores none. The schema says `counts_toward_ratio`, never
`qualified`, `licensed` or `certified`; responses carry `eligibility_basis:
OPERATOR_DESIGNATED`. The UI says "Counts toward configured classroom policy" and "VeoTrex does
not verify licences or qualifications". "Qualified staff" elsewhere in the domain keeps the
meaning from ADR 0024: the ratio slot for people who count toward the configured policy.

### Facility-wide eligibility

A designation is per (tenant, facility, staff profile). It is not per classroom: a teacher who
counts in the toddler room counts in the pre-K room of the same building, and which room they are
in is presence, not eligibility. Nothing in the existing requirements needed classroom-specific
eligibility.

A designation also *is* facility roster membership. A person may be on the roster and not count
(`counts_toward_ratio = false`, e.g. an assistant or a cook): they can be checked in and are shown
in the room, but are not counted. Check-in requires a designation in force at the classroom's
facility, so a tenant-wide profile never appears in a facility it was not placed in.

`staff_ratio_eligibility` columns: tenant, facility, staff profile, status ACTIVE / INACTIVE,
`counts_toward_ratio`, optional operator `note` (≤ 500 characters, free text, never audited
verbatim), `effective_from`, optional `effective_until` (entered as facility-local days, stored in
UTC, the same convention as ADR 0024 policies), `revision`, creator, deactivator, timestamps.

Rules:

- Only an ACTIVE profile can be designated or have a designation changed. An unknown,
  other-tenant or DELETED profile is a uniform 404, as in V1-02A.
- Composite FKs `(facility_id, tenant_id)` and `(staff_profile_id, tenant_id)` make a
  cross-tenant designation unstorable.
- **One ACTIVE designation per (tenant, facility, staff)** — the partial unique index
  `uq_staff_ratio_eligibility_active`. Ambiguous overlapping designations therefore cannot be
  written. If rows written outside the API ever produce two in force with different
  `counts_toward_ratio`, the resolver counts nobody for that person and reports it
  (`present_ambiguous`).
- A change bumps `revision` and audits before/after; a no-op is not a revision. Deactivation is
  idempotent. Rows are never deleted: the runtime role has SELECT, INSERT, UPDATE and no DELETE.
  A new period after a deactivation is a new row.

### Append-only presence events

`staff_presence_events` stores one row per operator action:

- tenant, facility, classroom (`area_id`), staff profile;
- `sequence`: the position in that person's stream (1, 2, 3, …);
- `event_type`: `CHECKED_IN`, `REFRESHED` or `CHECKED_OUT`;
- `source`: always `STAFF_ROSTER` (CHECK);
- `occurred_at` (server clock), `valid_until` (the lease; open events only), `checked_in_at`
  (start of the current stay, carried by REFRESHED and CHECKED_OUT);
- the recording actor and `created_at`.

There is no image, face, embedding, track, box or camera reference, and no column that could
hold one. The runtime role has **SELECT and INSERT only**: a refresh is a new event, so no event
is ever updated, and nothing is deleted. No trigger is needed; the grant is the guarantee.

CHECKs mirror the rules: event type and source; `sequence ≥ 1`; a CHECKED_OUT has no lease and an
open event must have one; lease 60 s – 4 h; `occurred_at` no more than 120 s after `created_at`;
`checked_in_at ≤ occurred_at` and equal to it on CHECKED_IN. The composite FK to
`areas (id, facility_id, tenant_id)` makes a classroom/facility mismatch unstorable.

**Current presence is derived, never stored.** A person's latest event by `sequence` decides:
CHECKED_IN / REFRESHED in room R with `now < valid_until` is PRESENT in R; past `valid_until` it
is STALE; CHECKED_OUT (or no event) is NOT_CHECKED_IN. Nothing older is ever reconsidered.

### Deterministic current room and concurrency

A person has at most one current room — tenant-wide, not only per facility — because the latest
event of one stream names one room. Transitions (`plan_check_in`, `plan_refresh`,
`plan_check_out`, pure):

| Prior state | Check-in to R (facility F) |
| --- | --- |
| not checked in, or checked out | CHECKED_IN R |
| STALE in R | CHECKED_IN R (a new stay) |
| PRESENT in R | 409 `staff_already_checked_in` — use refresh; a repeated click never silently resets a lease |
| PRESENT or STALE in another room of F | **atomic move**: CHECKED_OUT old room + CHECKED_IN R, same transaction, same timestamp, audited as `staff_presence.moved` |
| PRESENT in another facility | 409 `staff_checked_in_elsewhere` — this operator may not administer that facility; check out there first, or let it lapse |
| STALE in another facility | CHECKED_IN R; the new latest event supersedes the lapsed stay without writing in a facility this operator may not administer |

Check-out of R: open (PRESENT or STALE) in R → CHECKED_OUT, which also closes a lapsed stay on the
record; PRESENT elsewhere → 409 `staff_in_another_classroom`; otherwise an idempotent no-op
(200, nothing appended, nothing audited). Check-out is allowed for an inactive or deleted
profile, because closing a stay is always safe.

Refresh of R: only PRESENT in R, still ACTIVE, still designated; a new REFRESHED event with a new
lease from now, keeping `checked_in_at`. STALE → 409 `staff_presence_expired`: the person may
have left, so the operator checks them in again.

Check-in and refresh refuse an inactive profile, an inactive classroom or facility, and a person
with no designation in force at the classroom's facility.

**Concurrency.** `UNIQUE (tenant_id, staff_profile_id, sequence)` is the guarantee: two writers
that read the same state both try to append the same next position, and only one commits (the
other gets 409 `presence_state_changed`). A transaction-scoped advisory lock per person
(`staff_presence:{tenant}:{staff}`) turns that race into a wait: the second writer re-reads the
state the first left and decides again, with the clock read after the lock. A DB-backed test
fires eight simultaneous check-ins into two rooms and asserts a gap-free stream and exactly one
current room.

### Lease and freshness

A check-in is never trusted indefinitely. Default lease **15 minutes**, choosable 1 minute –
4 hours, renewed only by an explicit operator refresh. The existing conventions informed the
choice: a manual head count lasts 30 s – 15 min (default 2 min) because it describes a moment; a
staff member's stay is more stable than a head count, so a longer maximum is allowed, but the
default stays conservative, and the 4 h ceiling is well inside the engine's 12 h outer bound.
A person checked in yesterday and never checked out is STALE and never counted.

The roster count is recomputed at every evaluation and is FRESH by construction; its
`valid_until` is the earliest lease among the people counted, after which the count changes. The
web ratio card re-checks that on the browser clock every second and shows "Presence data stale"
until the server re-evaluates (every 15 s).

Attendance-clock integrations may later bring different freshness semantics; they are not part
of this stage.

### The authoritative staff count

`resolve_staff_count(classroom, facility, now, members, events, assignments)` is pure. A person
counts only when all hold:

- the staff profile exists (is in `members`) and is ACTIVE;
- their latest event is open in *this* classroom and facility, and the lease has not run out;
- a designation is in force at this facility now, it is unambiguous, and `counts_toward_ratio`
  is true.

Everyone else present is reported in a bounded bucket, with no names: `present`,
`present_ratio_ineligible`, `present_inactive`, `present_ambiguous`, `stale`, plus `count`,
`source = STAFF_ROSTER`, `freshness`, `valid_until`, `evaluated_at`. The function has no
parameter through which a camera count, a recognition result or an UNKNOWN person could enter.

### Source mode and source precedence

This is the first stage with two authoritative sources for one slot, so precedence is explicit
and per classroom: `areas.presence_source_mode`.

| Mode | CHILD | QUALIFIED_STAFF | VISITOR |
| --- | --- | --- | --- |
| `MANUAL_AGGREGATE` (default; every existing row) | MANUAL | MANUAL | MANUAL |
| `ROSTER_STAFF_PLUS_MANUAL_CHILDREN` | MANUAL | STAFF_ROSTER | MANUAL |

- STAFF_RECOGNITION: **never** authoritative in this stage.
- Vision / occupancy: **never** authoritative (unchanged from ADR 0024).
- ATTENDANCE: not connected; no precedence is defined for it yet.

`compose_presence` enforces this with an allow-list (`AUTHORITATIVE_SOURCES`): a count from any
other source is refused, even if smuggled into a manual resolution.

**Never manual staff + roster staff.** In roster mode the manual report carries children and
visitors only: the API refuses a staff number there (409 `staff_count_comes_from_roster`), and
`classroom_presence_snapshots.qualified_staff_count` became nullable to store that honestly. A
report made before the switch still carries its old staff number; in roster mode it is ignored
(and not shown), never added.

**Transitions are explicit.** `POST /v1/classrooms/{id}/presence-source-mode` (administer
facility) switches and audits `classroom.presence_source_mode_changed {from, to}`. Nothing
switches implicitly. Switching back to MANUAL_AGGREGATE does not borrow the roster's number: if
the latest manual report was made in roster mode it has no staff count, and the ratio reads
`INSUFFICIENT_DATA` / `STAFF_COUNT_MISSING` until an operator reports one. V1-04B snapshots stay
intact and auditable.

### Ratio engine integration

The engine (`evaluate_ratio`) is unchanged. The flow is: classroom → policy in force → latest
manual report (`resolve_manual_presence`) → in roster mode, `resolve_roster` →
`compose_presence` → one `PresenceSnapshot` → `evaluate_ratio`.

- A stale child report passes through as stale → `INSUFFICIENT_DATA` whatever the roster says;
  a revoked or missing one reads as missing. No earlier safe state is carried forward.
- No roster staff with children present is a real 0 → `OVER_CONFIGURED_RATIO`
  (`NO_QUALIFIED_STAFF_PRESENT`), never "insufficient".

`GET /v1/classrooms/{id}/ratio-status` adds `presence_source_mode` and `sources`:
`children`, `qualified_staff`, `visitors` (each `count`, `source`, `freshness`, `valid_until`;
the count only while usable) and `staff_roster` (the bounded resolver summary).

### API

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/v1/facilities/{facility_id}/staff-ratio-eligibility[?staff_profile_id=]` | read:operational on the facility |
| POST | same | administer:facility there |
| PATCH | `…/staff-ratio-eligibility/{id}` (`counts_toward_ratio`, `note`, `effective_through_date`) | administer:facility |
| POST | `…/staff-ratio-eligibility/{id}/deactivate` (idempotent) | administer:facility |
| GET | `/v1/classrooms/{classroom_id}/staff-presence` | read:operational |
| POST | `…/staff-presence/check-in` (201), `…/refresh`, `…/check-out` | administer:facility |
| POST | `/v1/classrooms/{classroom_id}/presence-source-mode` | administer:facility |

`administer:facility` rather than `manage:staff`: designations and check-ins are facility
operations, scoped to one facility, like ratio policies and manual reports; `manage:staff` is
tenant-owner-only and governs identity and biometric enrollment. A facility admin can therefore
roster tenant staff at their own facility and nowhere else. Status codes follow the classroom
API: unknown / other-tenant / unreadable identifiers 404 (uniform `not found`), readable but not
administrable 403, validation 422, state conflicts 409 with a bounded category. Request bodies
are `extra="forbid"`: no timestamp, image, track or face field can be sent.

### Recognition boundary

The V1-02B0 position is unchanged (ADR 0020): YuNet detection is production-acceptable; SFace
recognition is LOCAL_EVALUATION_ONLY; thresholds are evaluation values; templates are not
encrypted at rest; staging and production refuse the real backend and default to `unavailable`.

A camera recognising a person does **not** check them in, refresh them, move them, or count them.
Reasons:

1. Recognition cannot today be trusted as evidence at all (the blockers below).
2. Even a trustworthy match says "this face was seen here at this moment", not "this person is
   supervising this room until further notice". Presence is an accountable operator statement
   with a lease; a sighting is not.
3. An automatic check-in would let a misidentification *raise* a room's staff count — the
   failure direction that hides an understaffed room.

Enforced by construction and by tests (`test_staff_presence_recognition_boundary.py`): the
recognition modules import nothing from the roster and vice versa (AST checks); the resolver has
no recognition input; a real evaluation-route MATCH and UNKNOWN leave zero events and an
unchanged count, and only an explicit check-in changes it; the roster works with the fail-closed
backend where no recognition route exists; staging/production still refuse `opencv_eval`;
`compose_presence` refuses a STAFF_RECOGNITION count.

### Future production-recognition integration requirements

Before recognition may contribute to presence in any form, all of these are required, each with
its own ADR:

- **Production face-recognition blockers (must all be cleared):**
  - a production-approved recognition weight with established licence and training-data
    provenance (SFace: opencv_zoo issue 313 unresolved);
  - production-calibrated thresholds measured on representative, consented data;
  - biometric templates encrypted at rest (the option A AEAD design recorded for V1-02B0);
  - deployment and security qualification of the recognition path on the target hardware.
- A defined, audited contract for what a match may *suggest* (for example a prompt to an
  operator) versus *record*; recognition must never be the sole basis of a check-in.
- Its own source (`STAFF_RECOGNITION`) in the precedence table, with disagreement handling
  against the roster, and never able to raise a count without operator confirmation.
- No child face recognition, ever; UNKNOWN stays inert.

### Audit

Existing `audit_events`, IDs and state transitions only:

- `staff_eligibility.created`, `.updated` (before/after), `.deactivated`: facility, staff profile,
  `counts_toward_ratio`, period, `note_present` (not the note), revision;
- `staff_presence.checked_in`, `.moved` (`from_classroom_id`), `.refreshed`, `.checked_out`:
  classroom, facility, staff profile, source, event ids, previous state, lease and `valid_until`
  for open events, `counts_toward_ratio` at the time;
- `classroom.presence_source_mode_changed`: from, to.

No display name, note text, token, credential, image, frame, track, embedding or template.
Idempotent repeats are not audited again.

### Web

- Staff profile: "Facility roster and configured ratio" — per readable facility, whether the
  person is on the roster and "Counts toward configured classroom policy: Yes/No", with add,
  toggle and remove for administrators.
- Classroom: a "Staff presence" card — the source mode and a switch, the roster count with its
  not-counted buckets, each rostered adult with a counts/does-not-count badge, check-in time and
  a per-second "expires in" countdown, and check-in / move here / refresh / check-out controls
  with a lease choice; a bounded recent-events list.
- Manual presence card in roster mode: children and visitors only.
- Ratio card: "Children reported: 6 — Manual", "Qualified staff present: 2 — Staff roster",
  "Required qualified staff: 2", configured-policy headline. No legal-compliance wording.
- Adult staff names appear (they are already enrolled adult profiles). No child identity
  appears anywhere.

## Consequences

- A classroom can show *who* is supervising it and which of them count, without anyone typing a
  staff number, while children remain an aggregate manual count.
- Operators must refresh long stays (every 15 minutes by default, or choose up to 4 hours).
- `staff_presence_events` grows with every action; retention and archival are a later stage.
  Responses are bounded (last 20 events per classroom).
- The downgrade of migration 0011 refuses while roster-mode manual reports (NULL staff count)
  exist, rather than inventing a number.
- A V1-02A route guard that required every staff-named path to live under `/v1/staff` now
  enumerates the seven V1-04C roster paths explicitly; any other staff path outside `/v1/staff`
  still fails it.
- No alerts, no child attendance, no parent–child association, no recognition integration, no
  multi-camera fusion, no jurisdictional ratios and no legal-compliance claim.
