# ADR 0020: A local-evaluation face recognition backend

- Status: Accepted (V1-02B0)
- Date: 2026-09-22
- Supersedes nothing. Extends ADR 0019.

## Context

ADR 0019 left `FaceEnrollmentBackend` as a protocol with two implementations: a deterministic
fake for CI and local work, and an `unavailable` production default that refuses every upload,
because no face model with a licence acceptable for a commercial edge product had been
reviewed. That was the right place to stop, but it means nothing about the recognition pipeline
has ever been tried on a real face. Before building recorded-video person tracking on top of
it (V1-02B1), Chandra and consenting adult friends need to enroll themselves locally, submit a
separate photo, and see whether VeoTrex names the right person — and, more importantly, whether
it correctly declines to name the wrong one.

Two things stood between that and a model: which weights VeoTrex may legally load, and what
happens to real biometric templates once they exist.

## Decision

### The licence audit gates the model, and the weights are audited separately from the code

Three candidate families were reviewed against their upstream sources at fixed revisions. The
full record, with digests and evidence URLs, is in
`docs/qualification/v1-02b0-face-model-licence-audit.md`; the machine-readable form is the
registry in `veotrex_api.face_models`, which is what the application actually enforces.

- **YuNet** (`face_detection_yunet_2023mar.onnx`, opencv_zoo @ `4.10.0`) — the directory's own
  `LICENSE` is MIT and covers every file in it, weights included, with no field-of-use
  restriction. **PRODUCTION_APPROVED.**
- **SFace** (`face_recognition_sface_2021dec.onnx`, opencv_zoo @ `4.10.0`) — the directory
  declares Apache-2.0, but neither the README nor the pull request that introduced the December
  2021 weight names the dataset it was trained on, and opencv_zoo issue 313 (opened 2026-07-22)
  asks upstream to confirm that the declared licence covers commercial inference with these
  weights. It is still open with no maintainer answer. **LOCAL_EVALUATION_ONLY.**
- **InsightFace** pretrained models — the repository states plainly that the training data and
  the models trained on it are for non-commercial research only, with commercial use requiring
  a separate agreement. No such agreement has been obtained. **REJECTED**, and a test asserts
  that no InsightFace artifact appears in the registry under any name.

A code licence is not a model-weight licence. SFace's Apache-2.0 header is a statement about
the files in that directory; it is not upstream confirming the provenance of what those files
encode. Until that question is answered by upstream rather than inferred by us, the recognition
half of this backend is an evaluation tool and not a product component.

The alternative — YuNet plus a handcrafted descriptor such as an aligned multi-region LBP
histogram — was held in reserve and not built. SFace clears the local-evaluation bar, and a
qualification run against a descriptor VeoTrex would never ship would measure the wrong thing.

### The backend is `opencv_eval`, and three independent refusals keep it out of production

`OpenCvEvalFaceBackend` implements both `FaceEnrollmentBackend` and the new
`FaceRecognitionBackend`: YuNet detects and land-marks, `alignCrop` warps to the 112×112 SFace
expects, SFace produces a 128-float embedding, and the result is L2-normalised so cosine
similarity is a dot product. The stored model identity is the composite `yunet+sface` /
`2023mar+2021dec`, because a template is only comparable to another produced by the *same*
detector and the *same* embedding model.

It is refused outside `local` / `development` / `test` / `ci` three times over, and each
refusal is asserted by its own test, because the point of three is that removing one still
leaves the property true:

1. `Settings` refuses the backend name, so the process does not start.
2. `face_opencv.build` and the backend's constructor refuse the environment, so code that
   bypasses settings validation still cannot obtain one.
3. `ensure_ready` refuses to load a weight whose approval does not permit the environment.

The production default remains `unavailable`. `UnavailableFaceBackend` deliberately has no
`extract_query` method at all rather than one that raises: `supports_recognition` must be able
to say truthfully that it cannot recognise anyone, so that even a permitted environment
running the fail-closed backend registers no recognition route.

### Weights are operator-installed, digest-verified, and never downloaded by the application

Nothing in the application fetches a model — not at import, not at startup, not per request; a
test greps the face modules for fetch calls. The operator runs
`infra/local/fetch-face-eval-models.sh` once, which pins the opencv_zoo tag, downloads to a
temporary name, and promotes the file only after both its byte size and its SHA-256 match.
The digests come from the git-lfs pointers published at that tag, and a test asserts the script
and the in-code registry agree.

The registry lives in code rather than in a JSON manifest on disk, which is a deliberate
inversion of the edge agent's convention (`veotrex_edge_agent.model_artifacts`). On the edge, a
manifest travels with the artifact. Here, a file under the operator's model directory must
never be able to authorise a different weight than the one that was audited, so the digest is
part of the program. `VEOTREX_STAFF_FACE_MODEL_DIR` names a directory only; the file names come
from the registry, so no request or environment variable can choose a model, and `resolve`
refuses a symlink, a non-regular file, a wrong size or a wrong digest without ever naming the
path it looked in.

### Recognition is a separate service, and refusing to name someone is the default

`StaffRecognitionService` can create nothing, accept nothing and write nothing. Candidate
selection happens in SQL under the caller's RLS context so a filter cannot be forgotten: ACTIVE
profile, READY enrollment, ACTIVE template, matching model identity and version.

A teacher's score is the mean of their two best template similarities. Averaging the vectors
was rejected — it destroys the pose variation the three-to-five photos exist to capture, and a
mean vector's similarity has no calibrated meaning. Taking the single best template was
rejected as the most false-accept-prone choice available: one unlucky template that happens to
sit near a stranger would decide an identity on its own. Requiring two of a person's own photos
to agree is what turns a 0.8 similarity against one photo into an UNKNOWN.

Two guards then both have to pass: an absolute threshold (0.45), and a margin (0.06) between
the best and second-best *distinct staff*. The margin is the one that matters for safety — a
stranger who resembles two teachers about equally clears the threshold comfortably, and naming
the marginal winner would be exactly the wrong answer.

Both numbers are **evaluation values, not production-calibrated thresholds**, and nothing in
the code, the API response or the dashboard may present them as calibrated. They are stricter
than upstream's own verification threshold for SFace (cosine 0.363) because a false UNKNOWN is
always preferable to a false identity. Calibrating them needs a supervised run with several
consenting people, which is what this stage exists to make possible.

### Real templates are gated to local, not encrypted (option B)

V1-02A stores templates as `bytea` without dedicated encryption at rest. This stage introduces
real adult biometric templates, so that fact could not be carried forward silently. Of the two
acceptable options, **B was chosen**: real templates are hard-gated to local and development,
and staging and production stay on `unavailable` until encryption exists.

Option A — versioned AEAD encryption under a biometric-specific key, separate from the Ring
credential key — is the right end state and is deliberately not attempted here. It needs a
migration that changes the template column, a key-reference setting with its own escrow story,
and decryption in both the recognition path and the edge package builder; none of that could be
exercised against a real deployment in a stage whose entire premise is that nothing ships. The
follow-up is mechanical and its shape is recorded in the qualification document.

The residual risk is stated rather than mitigated: **anyone with read access to a local
development database holds recoverable face templates of the people enrolled in it.** That is
acceptable for a laptop and a Jetson holding three consenting adults, and it is not acceptable
for Hostinger, which is why the gate exists.

### The recognition-test surface does not exist where it must not

`POST /v1/staff/recognition-test` is registered only when the environment permits evaluation
*and* the running backend can recognise. In staging and production the route is never created,
so the path is simply not there. It is authenticated, requires `manage:staff` rather than a
read permission (submitting new biometric material for comparison is an operator power), and
is bounded by the same request middleware as an enrollment upload.

It persists nothing. The response carries a decision, a bounded score, the model identity, the
thresholds and `evaluation_only: true` — never an embedding, and never the identity of a
candidate it declined to name. The evaluation log line records the decision and the score and
deliberately not who was recognised: an evaluation must not accumulate a record of who was seen
when. The dashboard panel is gated a second time by `VEOTREX_FACE_EVALUATION_UI`, on the
server, so it is absent from the rendered page rather than hidden in the browser.

### The edge package can no longer be spilled

The package export is unchanged in shape, but from this stage it can carry real biometrics, so
its destination became part of the boundary. It is written to a file the tool creates itself,
exclusively, following no symlink, at mode 0600, and it refuses a path that already exists.
`--output /dev/stdout` therefore fails instead of printing every template to a terminal, a
shell history and any pipeline capturing the output. A partial file is removed if the write
fails, and only counts and a truncated revision are ever printed.

## Consequences

- Chandra can run the qualification described in
  `docs/qualification/v1-02b0-face-model-licence-audit.md` and produce real evidence about
  false matches before any video work begins.
- The control-plane image does not contain OpenCV: it is a PEP 735 dependency group, the image
  builds with `--no-dev`, and contract tests assert both.
- CI never needs a model weight. The gate, the registry, the matching arithmetic and the whole
  HTTP path are covered without one; the real-model suite skips when the weights are absent.
- Adopting SFace for production requires a new ADR, and specifically requires upstream to have
  answered issue 313 — or a different recogniser whose weights carry unambiguous commercial
  terms. Raising `opencv_eval` beyond `LOCAL_EVALUATION_ONLY` on the strength of the directory's
  Apache-2.0 header alone would be exactly the mistake this ADR exists to prevent.
- Face templates remain unencrypted at rest. Encryption is a precondition for any staging or
  production face backend, not a later improvement to it.
