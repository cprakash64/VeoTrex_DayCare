# Domain model

## Ownership hierarchy

```text
Tenant
└─ Facility (jurisdiction, IANA timezone)
   ├─ Area (room/building/other typed area; kind CLASSROOM = a classroom, V1-04A)
   │  ├─ ClassroomRatioPolicy (operator-configured, effective-dated; ADR 0024)
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

## Constraints not yet modeled

- Facility/building is represented by typed `Area`; whether Building deserves a separate entity is open.
- Actor roles are placeholders pending the authorization model.
- Tenant-specific activation of approved global policy versions requires a future effective-dated binding.
- Child, guardian, attendance, incident, and media entities are non-goals. Staff persons and their
  biometric templates were added in V1-02A (ADR 0019); classroom presence is modeled only as
  anonymous role counts from approved sources (ADR 0024), never as a child entity.
