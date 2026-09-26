# Domain model

## Ownership hierarchy

```text
Tenant
└─ Facility (jurisdiction, IANA timezone)
   ├─ Area (room/building/other typed area; kind CLASSROOM = a classroom, V1-04A)
   │  ├─ ClassroomRatioPolicy (operator-configured, effective-dated; ADR 0024)
   │  ├─ ClassroomPresenceSnapshot (operator-reported counts, append-only, expiring; ADR 0025)
   │  ├─ StaffPresenceEvent (operator check-in / refresh / check-out, append-only, leased; ADR 0026)
   │  └─ Zone
   │     └─ Camera
   ├─ CameraProviderConnection ── Camera
   └─ EdgeNode ── CameraAssignment ── Camera

Tenant ── Actor ── AuditEvent
JurisdictionPolicy ── PolicyVersion (global approved catalog)
```

Opaque UUID identifiers avoid predictable public IDs. Tenant ownership is explicit even where it
could be inferred. Composite foreign keys enforce matching ownership through the hierarchy.

`CameraProviderConnection.secret_ref` identifies a future secret-manager object. Provider device IDs
are opaque strings and unique inside a tenant/connection. `CameraAssignment` is historical: an end
timestamp closes it, and a partial unique index permits only one active assignment per camera.
`EdgeNode` records architecture, memory, GPU/accelerator capability, software version, heartbeat,
and status without assuming NVIDIA hardware.

Policy definitions are global catalog records, not customer-owned. `PolicyVersion` stores the exact
validated document, source metadata, effective date, status, and SHA-256 content digest. Future
safety-condition records must copy the policy-version ID and rule ID used; they must never resolve
“current policy” after the fact.

Deletion is restrictive. Core records transition to archived/disabled states; audit records do not
cascade. Permanent erasure requires an explicit, separately audited retention workflow that accounts
for legal holds and statutory requirements.

## Classrooms and configured ratio policy (V1-04A)

A classroom is an `Area` with `kind = 'CLASSROOM'` and an optional operator `age_band_label`; its
cameras are those whose zone belongs to it. `classroom_ratio_policies` holds the owner's
configured numbers (children per qualified staff member, minimum staff, optional group size) for
an effective period in facility-local days. These are configured policies, not verified law.
Ratio evaluation takes role counts only from approved presence sources; a camera's head count
is never a child or staff count and is used only as a reconciliation diagnostic. See ADR 0024.

The first connected presence source is MANUAL (ADR 0025): an operator reports children,
qualified staff and visitors for a room as an append-only, short-lived snapshot (30 s - 15 min,
default 2 min). Only the latest report can be authoritative; once revoked or expired the ratio
is `INSUFFICIENT_DATA` and no earlier report is reused.

A classroom's qualified-staff count can instead come from the staff roster (ADR 0026), chosen
explicitly per classroom (`presence_source_mode`). `StaffRatioEligibility` places a tenant-wide
`StaffProfile` on a facility's roster and records whether an operator designated them as counting
toward the configured classroom policy (not a verified qualification). `StaffPresenceEvent` rows
check them into a classroom for a bounded lease (default 15 min, max 4 h); a person's latest event
decides their single current room. Children stay a manual aggregate count, and face recognition
never checks anyone in.

## Constraints not yet modeled

- Facility/building is represented by typed `Area`; whether Building deserves a separate entity is open.
- Actor roles are placeholders pending the authorization model.
- Tenant-specific activation of approved global policy versions requires a future effective-dated binding.
- Child, guardian, attendance, incident, and media entities are non-goals. Staff persons and their
  biometric templates were added in V1-02A (ADR 0019); classroom presence is modeled only as
  anonymous role counts from approved sources (ADR 0024), never as a child entity.
