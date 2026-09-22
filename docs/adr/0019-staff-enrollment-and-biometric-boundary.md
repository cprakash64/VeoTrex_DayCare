# ADR 0019: Staff enrollment and the biometric boundary

- Status: Accepted (V1-02A)
- Date: 2026-09-22

## Context

VeoTrex must later recognise enrolled adult staff in recorded and live daycare video while
treating everyone else, and every child, as UNKNOWN. That requires a tenant-scoped identity for
each consenting teacher, a small set of enrollment photos, and face templates derived from
them. The existing `actors` / `actor_identities` tables represent authenticated dashboard users
bound to Auth0 subjects; they are not monitored people and must not be overloaded. No face
model with a licence acceptable for a commercial edge product has been reviewed yet (ADR 0009
set the precedent of refusing AGPL/commercial-only candidates), and no media or storage
abstraction existed in the control plane.

## Decision

### Separate staff domain, no child counterpart

`staff_profiles`, `staff_enrollment_images` and `staff_face_templates` are new tenant-owned
tables with forced Row Level Security and composite `(id, tenant_id)` foreign keys, following
migrations 0001 and 0004. There is deliberately no schema for child identities and the platform
never creates one: enrollment is an explicit action by a tenant owner in the dashboard, never
an inference from footage. `status` (ACTIVE / INACTIVE / DELETED) is the operator's decision;
`enrollment_state` (EMPTY / COLLECTING / PROCESSING / READY / FAILED) is computed and persisted
by the backend from the accepted images and their templates. A person is recognisable only
when ACTIVE and READY with at least three accepted photos; the UI never infers readiness.

### Photos are validated from bytes and stored privately

Uploads are raw JPEG or PNG bytes bounded at 8 MiB by the request middleware. The container is
sniffed, dimensions are bounded before any pixel is decoded, the image is decoded with a
pixel-count cap, EXIF orientation is applied, and a canonical RGB JPEG is produced with every
metadata block discarded. The face backend must see exactly one face of adequate size; identical
content is refused as a duplicate. Only the canonical copy is stored, in a private directory
owned by the API process, under an opaque server-generated key, with an atomic 0600 write. No
request can name a filesystem path; the only retrieval is a tenant-authorized route returning
the canonical JPEG with `Cache-Control: private, no-store`. Deleting a photo or a profile
removes the bytes and revokes the templates derived from them. The media directory is a named
Docker volume on the control plane; the database backup does not include it, which is recorded
as a residual risk until a media backup exists.

### Face backend is a protocol; the production default fails closed

`FaceEnrollmentBackend` (analyze, extract_template, model identity and versions) is the only
face-related dependency of the enrollment service. `FakeFaceBackend` is deterministic and
dependency-free for CI and local work; settings refuse it outside test/local environments.
`UnavailableFaceBackend` is the production default: every upload is refused with
`face_backend_unavailable`, so no image is ever accepted without validation and no template is
ever fabricated. Adopting a real model is a separate decision that must record its licence,
weights provenance and platform compatibility before `VEOTREX_STAFF_FACE_BACKEND` gains a new
value. Nothing downloads a model.

### Templates stay inside the database

Templates are stored as `bytea` with model id, model version, template version, dimensions and
dtype, tenant-scoped under forced RLS, and revoked by status rather than deleted. They are never
serialised by any dashboard route (the OpenAPI surface has no template path) and never logged.
The Ring credential vault is not reused: it is a credential-class AEAD store bound to provider
contexts, and forcing biometric templates into it would couple two unrelated data classes to
one key. Encryption at rest with a dedicated key is deferred to the stage that adopts a real
model, when templates first become genuine biometric data; today only fake templates can exist.

### The edge receives templates through a privileged package, not an HTTP route

`veotrex-staff-recognition-package` (admin identity, like `veotrex-provision`) exports one
tenant's ACTIVE + READY staff with their ACTIVE templates for a named model and version, opaque
ids, display names, deterministic ordering, a content-derived revision and a bounded size, as a
0600 JSON file the operator carries to the Jetson. The edge agent has no authenticated identity
toward the control plane yet, and the dashboard must never receive templates, so there is no
polling endpoint to abuse or expose. When an edge credential model exists, the same package
becomes the response body of an edge-authenticated route.

## Consequences

Tenant owners can enroll consenting adults today, and the lifecycle, isolation, validation and
audit trail are production-grade; recognition itself is NOT READY until a licence-approved face
model is adopted and the production backend value is set. Runtime-role grants for the three
tables are SELECT/INSERT/UPDATE only; the migration role keeps ownership. The `api` service
needs the media volume mounted and `VEOTREX_STAFF_MEDIA_DIR` writable by uid 10001.
