# ADR 0028: Guardian association and authorized child release

- Status: Accepted (V1-04E). Control plane only. No alerting.
- Date: 2026-09-26

## Context

ADR 0027 gave each facility a child roster and operator attendance check-in/out, and explicitly
left parent/guardian association and pickup authorization unbuilt. A daycare's most sensitive
daily action is handing a child to an adult. Staff need the system to answer one question at the
door - *who is allowed to collect this child, right now?* - and to keep an immutable record of
each handover.

This is a control-plane stage. **This stage does NOT implement parent/guardian face recognition**,
child face recognition, or any camera-based identification. No camera observation can authorize or
record a release.

## Decision

### An adult contact is a roster entry, not an identity proof

`guardian_contacts` is facility-scoped and holds exactly: `id`, `tenant_id`, `facility_id`,
`display_name` (1–120, same rules as a child's name), `status` ACTIVE / INACTIVE / ARCHIVED
(archived is terminal), optional identifier-shaped `external_reference` (unique per facility),
`created_by_actor_id`, `created_at`, `updated_at`. The term is *contact*: a parent, legal
guardian, grandparent, babysitter or family friend are all contacts. Nothing is inferred about who
they are.

**Data minimisation.** There is deliberately no photo, face, embedding, voice, identity-document
image or number, government id, date of birth, home address, camera or track column. **Phone and
email are also omitted**: nothing in the release decision needs them, facilities already hold
contact details in their own enrolment systems, and every extra field is extra breach surface. A
future external connector can key on `external_reference`. A test pins the exact column set.

Names are shown only to authorised operators, rendered as text, never logged, never in audit
metadata and never on any edge or Ring surface. Contacts are deactivated or archived, never
deleted by the runtime (no DELETE grant).

### Association: relationship is not authorization

`child_guardian_links` is the explicit many-to-many between a child and a contact:

| Column | Meaning |
| --- | --- |
| `relationship_label` (1–64) | the operator's own words ("Mother", "Family friend"); no legal meaning, never interpreted |
| `pickup_authorized` | the authorization, set explicitly (the API has no default) |
| `effective_from`, `effective_until` | half-open `[from, until)`; `until` optional; `until > from` (CHECK) |
| `status` ACTIVE / INACTIVE, `deactivated_at/by` | an ended association is never reactivated |
| `note` (optional, ≤ 200) | operator reference text; never audited |
| `revision` | + 1 on every change |

`relationship_label` and `pickup_authorized` are independent: `"Father"` with
`pickup_authorized = false` is representable and is **not** authorized. "Anyone associated with
the child" is never sufficient.

One ACTIVE link per (child, contact) pair (partial unique index, plus a per-pair advisory lock on
create/edit). Changing the period or the flag edits that link (revision + 1, bounded before/after
audited); ending it deactivates the row; a later association is a new row. Composite FKs make a
link to another facility's child or contact unstorable.

### Temporary authorization

A link with `effective_from` / `effective_until` authorizes only inside that window: before it the
decision is AUTHORIZATION_NOT_STARTED, at or after `until` AUTHORIZATION_EXPIRED. Times are stored
in UTC, entered and shown in the facility's timezone; the API requires an explicit UTC offset. No
older link is ever revived: only the ACTIVE link is considered, and deactivating it leaves the pair
ASSOCIATION_INACTIVE.

### The decision engine

`guardian_release.decide_pickup(child, contact, links, facility_id, at)` is pure. It receives ids,
statuses and link terms selected by id - never a name, a label, an image, a track or a recognition
result - and returns AUTHORIZED or the first failing reason, in this fixed order:

1. `FACILITY_MISMATCH` (child, contact or link not at the releasing classroom's facility)
2. `CHILD_INACTIVE`
3. `AUTHORIZED_PERSON_INACTIVE`
4. `NO_ASSOCIATION`
5. `ASSOCIATION_INACTIVE`
6. `ASSOCIATION_AMBIGUOUS` (two ACTIVE links - the database forbids it; fail closed)
7. `PICKUP_NOT_AUTHORIZED`
8. `AUTHORIZATION_NOT_STARTED` / `AUTHORIZATION_EXPIRED`

At the API boundary an unknown, other-tenant or unreadable contact or child is a uniform 404; the
engine's reasons are only returned (409, lower-cased category) about records the caller can read.

### Operator verification

A release requires the operator's statement of how they confirmed the adult:

- `KNOWN_TO_STAFF` - a staff member knows this adult;
- `OPERATOR_CONFIRMED` - the operator confirmed who they are;
- `PHOTO_ID_CHECKED` - the operator reports looking at an identity document. **VeoTrex scans,
  copies, stores and authenticates nothing**: no image, number or document detail is captured.

The suggested `OTHER_MANUAL` was deliberately left out: `OPERATOR_CONFIRMED` already covers any
other confirmation the operator stands behind, and a free-text "other" would invite identity
details into a note. VeoTrex is not a legal or identity-verification system; the list records
what the person at the door says they did.

### Append-only release events

`child_release_events` (runtime SELECT, INSERT only) records: facility, classroom, child, contact,
`authorization_link_id` and the link `revision` in force, `verification_method`, `released_at`,
`attendance_event_id`, recorder and `created_at`. No name, label, note, image or track is
duplicated into it. The database itself ties a release to its check-out: a composite FK to
`child_attendance_events (id, tenant, facility, classroom, child, event_type, occurred_at)` with
`attendance_event_type = 'CHECKED_OUT'` pinned by CHECK, `released_at` equal to the check-out
time, and `UNIQUE (tenant_id, attendance_event_id)`; a second composite FK ties the link to exactly
this child and this contact.

### The release transaction

`POST /v1/classrooms/{id}/attendance/release` with `{child_profile_id, guardian_contact_id,
verification_method}` (nothing else is accepted) runs one transaction:

1. load the classroom (administer:facility), the child (same facility) and the contact (readable);
2. take the child's attendance advisory lock - **the same lock** check-in, move, refresh and
   check-out take - then re-read the child, the contact and the pair's links `FOR SHARE`, so a
   concurrent status change or pickup toggle serialises with the release;
3. require the child PRESENT in this classroom (`child_not_checked_in`,
   `child_in_another_classroom`, or `attendance_expired` for a lapsed stay - a release record would
   claim a handover nobody saw, so a lapsed stay is closed by an administrative check-out);
4. decide (above);
5. append the CHECKED_OUT attendance event (the V1-04D `plan_check_out`);
6. append the release event referencing that event and the exact link revision;
7. append one audit row.

Any failure rolls back all of it: there is never a release without a check-out, nor (through this
endpoint) a check-out without a release. Tests force failures at the check-out, the release row,
a database refusal and the audit write.

### Concurrency

The per-child advisory lock turns racing releases into a queue: the second writer re-reads the
state and sees the child already checked out (409 `child_not_checked_in`). The UNIQUE per-child
attendance `sequence` and UNIQUE release-per-check-out remain the guarantees. A DB-backed test
fires ten simultaneous releases (two different authorized adults): exactly one 201, one
CHECKED_OUT, one release row; others 409. A release racing an administrative check-out, and a
release racing a pickup-disable, are also tested.

### Administrative check-out vs authorized release

The existing `POST .../attendance/check-out` is kept, unchanged in behaviour, as an
**administrative check-out** for corrections and lapsed stays. It never creates a release record
and never names an adult. The distinction is structural, not a label on old rows:

- a CHECKED_OUT event is an authorized release **iff** a `child_release_events` row references it;
  historical check-outs have none, so none can look like a release;
- the attendance history response marks `released: true` only on those events;
- audit: a direct check-out's `attendance.checked_out` metadata now carries
  `checkout_kind: ADMINISTRATIVE_CHECKOUT`; a release writes `child.released` with
  `checkout_kind: AUTHORIZED_RELEASE` (and no separate `attendance.checked_out`).

The web classroom card makes "Release child" the normal action and moves the direct check-out
under "Correction" as "Administrative check-out".

### Audit

`audit_events` rows: `guardian.created/updated/activated/deactivated/archived`,
`child_guardian_link.created/updated/pickup_enabled/pickup_disabled/deactivated`,
`child.released`, and `attendance.checked_out` (administrative). Metadata holds ids, statuses,
flags, periods, revisions, the verification method and `changed_fields` names - **never** a
contact or child name, a relationship label, a note, an external reference, a phone, an email, an
image or a camera reference. Audit rows are written in the same transaction as the change.

### Access

Read: `read:operational`. Contact roster, links and release: `administer:facility` (the same
permission as attendance; no narrower permission exists yet). Unknown, other-tenant or unreadable
resources: uniform 404; readable but not administrable: 403; validation: 422; state conflict or
refusal: 409. All routes are human (Auth0) routes; an edge machine credential opens none of them.

### Web

- **Guardians & contacts** page per facility: add, rename, set/clear reference, deactivate,
  reactivate, archive. No photo, document or contact-detail field.
- **Child page** (from the child roster): *Authorized pickup people* - name, relationship label,
  "Authorized for pickup" / "Not currently authorized" with reason, dates in facility time,
  status; add an association (contact and pickup decision both explicit, nothing pre-selected),
  edit label/dates/note, enable/disable pickup, deactivate; and the child's release history.
- **Classroom attendance**: per present child, *Release child* opens a form: (1) choose one of the
  adults authorized right now (others are listed read-only with the reason and cannot be chosen),
  (2) choose the verification method, (3) tick the explicit confirmation naming the child and the
  adult, then submit. Nothing defaults, nothing auto-submits; the submit button stays disabled
  until all three are done.

### Policy packs

`PolicyPack.attendance_release_rules` exists as an untyped list and is empty in the only pack
(US-AZ). It was audited and **not** enforced: there is no verified mechanism for turning pack
entries into enforced rules, and VeoTrex makes no legal determination. Facilities configure who
may collect a child; the system records and enforces exactly that.

## Edge / camera boundary

No edge change. Tests prove: no edge-agent source names a guardian, pickup or release; no
camera, Ring, edge or face module imports or names guardian code, and the guardian modules import
nothing from them; no guardian route lives under `/v1/edge` or `/v1/integrations`, serves or
accepts an image, or has a photo/face/recognition path; the release request schema is exactly
three fields; an edge-style token gets 401 on guardian routes; a real evaluation-route face
MATCH (and an UNKNOWN) releases nobody and checks nobody out; no kinship, resemblance or facial
relationship identifier exists anywhere in the API. Guardian and child records are never sent to
the Jetson.

## Future boundaries (not implemented)

- **Parent portal**: an external parent-facing app (self-service contact updates, pickup
  notifications) needs its own identity model for non-staff users, consent capture and its own
  ADR. It must not reuse operator permissions, and parents must never see other families' data.
- **External attendance / provider integration**: a connector may key on the contact's and
  child's `external_reference` to import associations or record releases through the same pure
  decision and the same append-only tables, with its own actor identity; it must never infer
  identity or relationships from cameras.
- **Retention**: release history grows without bound; archival and retention periods (and any
  jurisdictional record-keeping duty) are a later stage.
- **Biometrics**: none. Any future proposal for guardian or child biometric identification would
  need a new ADR, legal review and consent design; it is out of scope for this product line today.

## Consequences

- Staff can answer "who may collect this child now?" and every handover is recorded immutably
  with the exact authorization that permitted it.
- Operators must keep associations current; an expired or disabled authorization blocks release
  until an administrator updates it (or the child leaves via an administrative check-out, which is
  visibly not a release).
- Migration 0013 adds three RLS-forced tables and one unique key on attendance events. Its
  downgrade refuses while any contact, link or release exists.
