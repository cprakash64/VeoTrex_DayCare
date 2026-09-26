# ADR 0024: Classroom and configured staff-to-child ratio policy foundation

- Status: Accepted (V1-04A). Foundation only: no presence source, no alerts, no legal status.
- Date: 2026-09-25

## Context

VeoTrex's live path (Ring → Jetson → YOLOX → tracker → occupancy) counts **people**. A daycare
needs to know whether each classroom has enough qualified staff for the children in it. The
owner will supply the ratios for each classroom; VeoTrex must not invent them, must not claim a
legal status for them, and must not guess who is a child.

The audit found:

- `Facility → Area (kind, default 'ROOM') → Zone → Camera`, with `cameras.zone_id` nullable.
- No classroom table, and no API for facilities, areas or zones; the runtime role had no access
  to any of them.
- A pydantic `PolicyPack` validator and an Arizona pack file, but no evaluator. The global
  `jurisdiction_policies`/`policy_versions` tables were unused.
- `staff_profiles` for enrolled adult teachers, but no attendance, check-in or presence model.
- Authorization already supported facility-scoped grants.

## Decision

### A classroom is an Area

A classroom is an existing `areas` row with `kind = 'CLASSROOM'`. Area is the typed room entity
the domain model already defines. A Zone is a camera-view subdivision *inside* a room, and a
ratio applies to the whole room, which can contain several zones and cameras. So no Classroom
table and no competing hierarchy were created.

Additions:

- **`areas.age_band_label`** (nullable): operator text such as "Toddler" or "Pre-K". It is
  never inferred from imagery, is not a child's age, and is not a name.
- **Camera association** is read through the existing `camera.zone_id → zone.area_id` path. It is
  view-only in this stage, because writing `zone_id` changes Ring inventory status semantics.
- **Timezone** is inherited from the facility. Policy dates are entered as local calendar days
  and stored as a half-open UTC period.

### Configured classroom ratio policy

`classroom_ratio_policies` (migration 0009) is tenant-owned, with forced RLS and composite
tenant-safe foreign keys. Columns:

- `label`, optional `age_band_label`
- `max_children_per_staff` (> 0), `minimum_staff` (≥ 0), optional `maximum_group_size` (> 0)
- `effective_from`, optional `effective_until` (must be > `effective_from`)
- `status` (`ACTIVE` / `INACTIVE`), `revision` (≥ 1)
- operator `source_reference`, `created_by_actor_id`, timestamps

Integer CHECKs mirror the service validation.

- **Overlap:** overlapping `ACTIVE` periods for one classroom are refused. The service takes a
  per-classroom transaction advisory lock, so two concurrent writers cannot both pass the check.
  Adjacent periods are allowed. If an overlap ever exists anyway (for example, rows written
  outside the API), selection is deterministic: the latest `effective_from`, then the highest
  revision, then the id. The ambiguity is reported as `POLICY_SELECTION_AMBIGUOUS`.
- **Edits** update in place, increment `revision`, and audit before and after values.
  Deactivation is permanent (a new policy replaces it), and rows are never deleted.
- **No jurisdictional numbers** are stored, seeded or hard-coded. Everything user-facing says
  "configured classroom policy". The global policy-pack tables remain the future path for a
  separately verified jurisdiction pack.

### Roles come only from approved sources

`PresenceRole` is `QUALIFIED_STAFF`, `CHILD`, `VISITOR` or `UNKNOWN`. `PresenceSource` is
`MANUAL`, `ATTENDANCE`, `STAFF_ROSTER`, `STAFF_RECOGNITION` or `OTHER_APPROVED_SOURCE`. **There
is no vision or detector source.** Each source may assert only certain roles:

- Staff recognition and the staff roster may assert only `QUALIFIED_STAFF`, never a child.
- Attendance may assert children, visitors and unknown, never staff.

Every `PresenceCount` carries a classroom, a role, a bounded count, its source, a UTC source
timestamp and a bounded validity.

**UNKNOWN is inert.** An UNKNOWN count is never a child and never qualified staff.
`PresenceSnapshot` has a typed slot per role and refuses a count of the wrong role in a slot.

**The camera is not a presence source.** A camera count is a separate type, `VisionObservation`.
`evaluate_ratio` refuses it, and it can only be passed to the reconciliation diagnostic.
`children = observed_people - recognised_staff` would turn every unidentified adult into a child
and every missed child into slack in the ratio, so no code path computes it. A test scans the
source for such a derivation.

### Pure ratio engine

`veotrex_api.classroom_ratio` is deterministic, has no I/O, and takes `now` explicitly.

    required_staff = max(minimum_staff, ceil(children / max_children_per_staff))  if children > 0
                   = 0                                                              otherwise

States:

- `NOT_CONFIGURED`: no policy in effect, or the classroom is inactive.
- `INSUFFICIENT_DATA`: a child or staff count is missing or stale.
- `NO_CHILDREN_PRESENT`: never a violation, even with zero staff.
- `WITHIN_CONFIGURED_POLICY`
- `OVER_CONFIGURED_RATIO`: includes children present with no staff, stated as
  `NO_QUALIFIED_STAFF_PRESENT`.
- `OVER_CONFIGURED_GROUP_SIZE`: group size means the children present.

`conditions` lists every exceeded limit, so being over both the ratio and the group size is
reported as both. There is no division by zero.

### Freshness

A count is fresh when `observed_at <= now < observed_at + valid_for`. A source timestamp more
than 120 s in the future is treated as stale. Validity is bounded to between 1 s and 12 h.

If either the child count or the staff count is missing or stale, the result is
`INSUFFICIENT_DATA`. Neither the last safe answer nor the last violation is carried forward,
and no count is shown.

Evaluation is stateless and compares source-provided UTC timestamps. A future long-running
consumer should use monotonic time for its own expiry.

### Vision reconciliation (diagnostic only)

`reconcile_vision` compares `children + qualified staff (+ visitors when supplied)` with the
camera's observed people. It returns `AGREES`, `VISION_LOWER_THAN_ROSTER`,
`VISION_HIGHER_THAN_ROSTER` or `NOT_AVAILABLE` (when vision or presence is missing or stale). It
also returns `unexplained_observed_people` and `unseen_expected_people`.

It returns a new frozen value and has no path back into a count or an evaluation. For example,
with 6 children, 1 staff member and 8 people seen, the ratio stays 6 : 1 and the extra person is
reported as one *unexplained* person, not a child.

### What is connected today

(Superseded for the control plane by ADR 0025, which connects MANUAL operator-reported presence.
The live edge dashboard remains unconnected.)

Nothing supplies presence yet:

- `GET /v1/classrooms/{id}/ratio-status` returns `INSUFFICIENT_DATA` (or `NOT_CONFIGURED`) with
  `presence_connected: false`, and reconciliation `NOT_AVAILABLE`.
- The web page says "Presence counts not connected".
- The live edge dashboard shows an inert card with the same wording. It presents the camera count
  as "people seen by camera (not a presence record)" and keeps that page's rule of using no
  demographic words.

The engine is not duplicated into the edge agent. When presence exists, the control plane is the
authority.

### API, access and data safety

The routes are:

- `GET /v1/facilities`
- `GET|POST /v1/classrooms`, and `GET|PATCH /v1/classrooms/{id}`
- `POST /v1/classrooms/{id}/activate|deactivate`
- `POST /v1/classrooms/{id}/ratio-policies`, `PATCH …/{policy_id}`, `POST …/{policy_id}/deactivate`
- `GET /v1/classrooms/{id}/ratio-status`

Access rules:

- Reads need `read:operational` on the classroom's facility; changes need `administer:facility`
  there. Tenant owners hold both.
- Unknown, other-tenant and unreadable-facility identifiers all answer the same 404. A readable
  classroom that the caller cannot administer answers 403.
- Every change is audited with numbers and labels only.

Runtime role changes:

- `facilities` and `zones`: SELECT.
- `areas` and `classroom_ratio_policies`: SELECT, INSERT, UPDATE.
- DELETE is never granted.
- No SECURITY DEFINER function was added.

No personal data is involved: no child entity, no names, no images, no faces, no persistent
person identity.

## Consequences

- Operators can model classrooms and enter the owner's ratios today, with honest "not
  connected" status until an approved presence source exists.
- Each future integration plugs into `PresenceCount` with its own source and allowed roles:
  - attendance or check-in (children, visitors);
  - staff roster or scheduling, and enrolled-teacher recognition (`QUALIFIED_STAFF` only);
  - manual room confirmation.
- Alerts are a separate stage. They must consume `RatioEvaluation`, never raw vision.
- A verified jurisdiction pack (ADR 0003) may later annotate a configured policy. Until then,
  nothing is called compliant or legal.
- Limits: at most 200 classrooms per facility and 100 policies per classroom.
- No multi-camera room fusion exists, and the camera association is read-only.

## Not in scope

Teacher-recognition integration, check-in/out, alerts, behaviour detection, unsupervised-child
logic, multi-camera fusion, and any claim of legal compliance.
