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

## Face backend

`VEOTREX_STAFF_FACE_BACKEND=unavailable` (production default) refuses uploads with
`face_backend_unavailable`; `fake` is deterministic and allowed only in test/local
environments. Real template generation is NOT READY until a licence-reviewed model is adopted
(ADR 0019).
