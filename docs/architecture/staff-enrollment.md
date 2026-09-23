# Staff enrollment (teachers)

## Scope

Adult staff only. A tenant owner enrolls a consenting teacher by name and three to five photos;
VeoTrex derives face templates so later stages can label the teacher in recorded or live video.
Nobody else is enrolled: there is no child identity model and no enrollment from footage. When
recognition confidence is insufficient the answer is UNKNOWN.

## Lifecycle

| Operator action | Route | Effect |
|---|---|---|
| create | `POST /v1/staff` | profile ACTIVE / EMPTY; audit `staff.created` |
| rename | `PATCH /v1/staff/{id}` | `staff.renamed` |
| add photo | `POST /v1/staff/{id}/enrollment-images` (raw JPEG/PNG body) | validated, canonicalised, stored, templated; `staff.image_accepted` |
| list / view photo | `GET …/enrollment-images`, `GET …/enrollment-images/{image}/content` | metadata; canonical JPEG, private |
| remove photo | `DELETE …/enrollment-images/{image}` | image DELETED, bytes removed, template REVOKED; `staff.image_removed` |
| deactivate / activate | `POST /v1/staff/{id}/deactivate`, `…/activate` | status INACTIVE / ACTIVE; `staff.deactivated` / `staff.activated` |
| delete | `DELETE /v1/staff/{id}` | status DELETED, all images DELETED and bytes removed, all templates REVOKED; `staff.deleted` |

`manage:staff` (tenant owner) is required for every mutation; `read:operational` reads the
roster, a profile, photo metadata and photo bytes. Unknown and other-tenant identifiers answer
404 identically. Uploads are rate limited per process.

## Readiness state machine

```text
EMPTY --photo--> COLLECTING --3rd accepted photo, all templated--> READY
COLLECTING/READY --remove below 3--> COLLECTING            READY --remove--> READY (>=3)
any --accepted photo without a usable template--> FAILED
(PROCESSING is held only inside the upload transaction)
recognition_ready = status ACTIVE and enrollment_state READY
```

The backend recomputes `enrollment_state` on every mutation from persisted rows: accepted
images and ACTIVE templates whose model id, model version and template version match the
configured backend. A model change therefore turns READY profiles into FAILED until their
photos are re-templated, and the UI shows the backend's word rather than a count.

## Validation categories

`empty_upload`, `file_too_large`, `unsupported_type`, `invalid_image`, `image_too_large`,
`image_too_small`, `no_face_detected`, `multiple_faces`, `face_too_small`, `duplicate_image`,
`enrollment_limit_reached`, `face_backend_unavailable`, `template_failed`,
`profile_not_active`. Every rejection is `422` with `{"detail": {"category": …}}`; a wrong media
type is `415`; an oversized body is `413` from the middleware before any decoding.

## Storage and retention

Canonical JPEGs live under `VEOTREX_STAFF_MEDIA_DIR/<tenant_id>/<32-hex-key>.jpg`, 0600 in
0700 directories, written atomically. Rows keep the key, dimensions, size, content hash and
the face backend's size/quality observation. Deleting a photo or profile unlinks the bytes after
the transaction commits; a file whose row no longer carries its key is unreachable. Templates
are `bytea` rows revoked by status; nothing is hard-deleted, so audit history stays intact.
The media directory is not part of the database backup (residual risk, ADR 0019).

## Edge contract (V1-02B)

`veotrex-staff-recognition-package --tenant-id … --model-id … --model-version … --template-version … --output file`
produces:

```json
{"schema_version": 1, "tenant_id": "…", "model_id": "…", "model_version": "…",
 "template_version": 1, "revision": "<sha256>", "generated_at": "…",
 "staff": [{"staff_id": "…", "display_name": "…",
            "templates": [{"template_id": "…", "dimensions": 128, "dtype": "float32",
                           "quality": 80, "data_base64": "…"}]}]}
```

Only ACTIVE + READY profiles, only ACTIVE templates for the named model, ordered by id, at most
500 staff and 5 templates each, no image bytes. `revision` changes whenever an included row
changes, so a consumer compares one string instead of polling rows. Deactivation, photo
removal below the minimum and deletion all remove a teacher from the next package.

The package is written to a file the tool creates itself, exclusively, following no symlink,
at mode 0600, and it refuses a path that already exists. `--output /dev/stdout` therefore fails
rather than printing real templates to a terminal or a shell history (V1-02B0); only counts and
a truncated revision are printed.

## Face backend

| `VEOTREX_STAFF_FACE_BACKEND` | Where it may run | What it produces |
|---|---|---|
| `unavailable` | anywhere; the production default | nothing — every upload is refused with `face_backend_unavailable`, and it cannot recognise at all |
| `fake` | local, development, test, ci | deterministic hash vectors. Not a biometric |
| `opencv_eval` | local, development, test, ci | **real adult biometric templates** — YuNet 2023mar + SFace 2021dec |

`opencv_eval` (V1-02B0, ADR 0020) is LOCAL_EVALUATION_ONLY: SFace's commercial weight
provenance is unresolved upstream, and face templates are not yet encrypted at rest. It is
refused outside the permitted environments three times over — by `Settings`, by the backend's
own constructor, and by `ensure_ready` refusing an evaluation-only weight — each asserted by
`apps/api/tests/test_face_environment_gate.py`. Weights are installed by the operator with
`infra/local/fetch-face-eval-models.sh` and verified by SHA-256 against the in-code registry
before loading; the application never downloads a model. OpenCV is a dependency group, so the
control-plane image does not contain it.

Templates are 128 × float32, L2-normalised, under the composite model identity
`yunet+sface` / `2023mar+2021dec`. Production remains `unavailable`; encryption at rest is a
precondition for changing that, not a later improvement.

## Local recognition test (V1-02B0, evaluation only)

`POST /v1/staff/recognition-test` takes a raw JPEG or PNG body and answers MATCH or UNKNOWN.
It is registered only where the environment permits evaluation *and* the running backend can
recognise, so in staging and production the route does not exist. It requires `manage:staff`,
is bounded by the same body and media-type middleware as an enrollment upload, persists
nothing, and returns a decision, a bounded score, the model identity, the thresholds and
`evaluation_only: true` — never an embedding, and never the identity of a candidate it declined
to name.

Candidates are ACTIVE profiles with READY enrollment and ACTIVE templates matching the running
model, selected in SQL under the caller's RLS context. A teacher's score is the mean of their
two best template similarities; naming anyone requires clearing an absolute threshold (0.45)
*and* a best-versus-second-best margin across distinct staff (0.06). Both are evaluation
values, not production-calibrated, and the dashboard says so wherever it shows one. The
dashboard panel is gated again, server-side, by `VEOTREX_FACE_EVALUATION_UI=1`.

See `docs/qualification/v1-02b0-face-model-licence-audit.md` for the licence evidence, the
measured Jetson figures and the operator test procedure.
