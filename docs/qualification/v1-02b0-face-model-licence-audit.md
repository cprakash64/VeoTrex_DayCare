# V1-02B0: face model licence audit, Jetson compatibility and the local test procedure

Audit date: 2026-09-22. Decisions recorded in [ADR 0020](../adr/0020-local-face-recognition-evaluation-backend.md).
The enforced form of this audit is the registry in `apps/api/src/veotrex_api/face_models.py`;
this document is the evidence behind it. Neither may be changed without the other — a test
asserts they agree with the operator fetch script.

## 1. Licence audit

Every field below was read from the named upstream at the named revision on the audit date, not
recalled. Digests are the SHA-256 published by that revision's git-lfs pointer, and were
independently confirmed by hashing the downloaded files.

### A. YuNet face detector — PRODUCTION_APPROVED

| | |
|---|---|
| Source repository | https://github.com/opencv/opencv_zoo |
| Revision | `4.10.0` (release tag) |
| File | `models/face_detection_yunet/face_detection_yunet_2023mar.onnx` |
| SHA-256 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| Bytes | 232,589 |
| Declared licence | MIT |
| Licence file | `models/face_detection_yunet/LICENSE` — "MIT License, Copyright (c) 2020 Shiqi Yu" |
| Training data | WIDER FACE, per the author's upstream libfacedetection project. The zoo directory does not restate it. |
| Commercial use | Permitted. MIT covers every file in the directory, weights included, with no field-of-use restriction and no separate model terms. |
| **Approval** | **PRODUCTION_APPROVED** |

MIT is unambiguous about the files it covers and the directory's README states that all files
in it are under that licence. There is no separate weights agreement to reconcile.

### B. SFace face recogniser — LOCAL_EVALUATION_ONLY

| | |
|---|---|
| Source repository | https://github.com/opencv/opencv_zoo |
| Revision | `4.10.0` (release tag) |
| File | `models/face_recognition_sface/face_recognition_sface_2021dec.onnx` |
| SHA-256 | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |
| Bytes | 38,696,353 |
| Declared licence | Apache-2.0 |
| Licence file | `models/face_recognition_sface/LICENSE` — Apache License 2.0 |
| Architecture | MobileFaceNet trained with the SFace loss, converted from https://github.com/zhongyy/SFace |
| Training data | **UNRESOLVED.** The upstream SFace project mentions CASIA-WebFace, VGGFace2 and MS1MV2, but neither the zoo README nor the pull request that introduced the December 2021 weight maps this specific ONNX file to a dataset. |
| Commercial use | **AMBIGUOUS.** See below. |
| **Approval** | **LOCAL_EVALUATION_ONLY** |

The blocking evidence is [opencv_zoo issue 313](https://github.com/opencv/opencv_zoo/issues/313),
"Commercial-use and training-data clarification for SFace 2021dec ONNX weights", opened
2026-07-22. It asks upstream to confirm that the declared Apache-2.0 covers commercial
inference with these weights and to state their training-data provenance. As of the audit date
it is **open with no maintainer answer**.

The distinction that decides this: the Apache-2.0 header is a statement about the files in that
directory. It is not upstream confirming the provenance of what those files encode, and the
datasets the upstream project names carry research-use restrictions of their own. Treating the
code licence as settling the weight licence is precisely the error this audit exists to avoid,
so the recognition half of the backend is an evaluation tool until upstream answers.

**Reassess when:** issue 313 receives an authoritative upstream answer. A maintainer confirming
commercial use and naming a permissively-licensed training set would allow a new ADR raising
this to PRODUCTION_APPROVED. Anything less does not.

### C. InsightFace pretrained recognition models — REJECTED

| | |
|---|---|
| Source repository | https://github.com/deepinsight/insightface |
| Code licence | MIT, with no restriction on academic or commercial use |
| Model licence | Non-commercial research only |
| Upstream statement | "The training data containing the annotation (and the models trained with these data) are available for non-commercial research purposes only." The repository directs commercial users to `recognition-oss-pack@insightface.ai` for open-sourced recognition models. |
| Commercial licence obtained | **No** |
| **Approval** | **REJECTED** |

This is the clearest case of a code licence differing from a model licence: MIT code,
non-commercial weights, stated by upstream in the same document. No InsightFace weight may be
introduced unless a commercial model licence has actually been obtained, and a test asserts
that no InsightFace artifact appears in the registry under any name.

### Conclusion

YuNet detection is usable anywhere. No recogniser is production-approved. SFace clears the
local-evaluation bar, so the fallback plan of YuNet plus a handcrafted descriptor (aligned
multi-region LBP histogram) was held in reserve and **not built**: qualifying a descriptor
VeoTrex would never ship would measure the wrong pipeline.

## 2. Backend

`opencv_eval` — YuNet 2023mar detection plus SFace 2021dec recognition.

| | |
|---|---|
| Model id | `yunet+sface` |
| Model version | `2023mar+2021dec` |
| Template version | 1 |
| Template | 128 × float32, little-endian, L2-normalised (512 bytes) |
| Similarity | Cosine, i.e. a dot product of unit vectors, bounded to [-1, 1] |
| Status | **LOCAL_EVALUATION_ONLY** |

The identity names both weights because a template is comparable only to another produced by
the same detector *and* the same embedding model. Changing either invalidates every stored
template, and the recognition query filters on model id, model version and template version
before it compares anything.

## 3. Evaluation thresholds

| Setting | Default | Meaning |
|---|---|---|
| `VEOTREX_STAFF_RECOGNITION_THRESHOLD` | 0.45 | Minimum per-teacher aggregate score to name anyone |
| `VEOTREX_STAFF_RECOGNITION_MARGIN` | 0.06 | Minimum gap between the best and second-best *distinct* teacher |
| `VEOTREX_STAFF_FACE_MIN_AREA_RATIO` | 0.015 | Minimum share of the frame the face must occupy |
| `VEOTREX_STAFF_FACE_MIN_SHARPNESS` | 20.0 | Variance-of-Laplacian floor on the aligned crop; an extreme-blur guard, not a quality score |
| `VEOTREX_STAFF_FACE_DETECTION_CONFIDENCE` | 0.9 | YuNet score floor |

**These are evaluation values. They are not production-calibrated and must not be described as
such.** Upstream verifies SFace pairs at cosine 0.363; 0.45 is deliberately stricter, because a
false UNKNOWN is always preferable to a false identity. Calibrating them requires the
supervised run in section 6 — that is what this stage exists to enable, and the panel shows the
raw score and the threshold precisely so the operator can see how close each decision was.

### Aggregation

A teacher's score is the **mean of their two best template similarities** (the single score
when only one template is usable). Rejected alternatives, and why:

- *Averaging the vectors* destroys the pose variation the three-to-five photos exist to
  capture, and a mean vector's similarity to a query has no calibrated meaning.
- *Taking the best template* is the most false-accept-prone option available: one template
  that happens to sit near a stranger decides the identity alone. A stranger scoring 0.8
  against one photo and ~0.09 against the rest would be named under max, and scores 0.44 —
  below the threshold — under top-two. That case is asserted as a test.

Then both guards must pass: the absolute threshold, and the best-versus-second-best margin
across distinct staff. The margin is the safety-critical one — a stranger resembling two
teachers about equally clears the threshold comfortably, and naming the marginal winner would
be the worst available answer.

Ordering is total (score descending, ties broken on staff id), so a tie produces a reproducible
"best" that the margin then refuses to return anyway.

## 3a. Pre-flight smoke run (not a calibration)

Run on 2026-09-22 against the real models, the real HTTP surface and a real PostgreSQL with
RLS, using faces from the recorded-video validation footage already in the tree. It exists to
prove the pipeline is wired correctly before a person is asked to sit for photographs; it is
**one genuine pair and one impostor pair**, which says nothing about the impostor distribution
and does not calibrate anything.

| Case | Result | Score |
|---|---|---|
| Three crops of one face enrolled | READY, recognition_ready true | — |
| A fourth, never-enrolled crop of that face | MATCH, correct person | 0.9837 |
| A different real face, not enrolled | UNKNOWN (below threshold) | 0.0536 |
| Image with no face | Refused: `no_face_detected` | — |
| Image showing two people | Refused: `multiple_faces` | — |
| Same photo after deactivating the teacher | UNKNOWN (`no_candidates`) | — |

The genuine and impostor scores are far apart and the 0.45 threshold sits well clear of both.
That is encouraging and it is not evidence about real enrollment conditions: both faces came
from upscaled video stills of the same scene type, and two pairs cannot describe a
distribution. Section 6 is the run that produces real evidence.

## 4. Jetson Orin Nano compatibility

Measured on the target host (`veotrex`) on 2026-09-22, CPU path, OpenCV from the PyPI
`manylinux` aarch64 wheel. These are the only performance figures in this document and they
were measured, not estimated.

| | |
|---|---|
| Architecture | aarch64 — supported; `opencv-python-headless` 4.14.0.94 publishes an aarch64 wheel |
| L4T | R39 revision 2.0 |
| Python / NumPy | 3.12.3 / 2.4.6 |
| OpenCV build | 4.14.0, CPU + OpenCL. **No CUDA**: `cv2.cuda.getCudaEnabledDeviceCount()` returns 0 |
| Model size on disk | YuNet 227 KiB, SFace 36.9 MiB |
| Both models load | 162 ms |
| Detect only (672×675) | median 41.0 ms, p95 49.9 ms (n=20) |
| Detect + align + embed | median 93.3 ms, p95 122.6 ms (n=20) |
| Peak process RSS | 218 MiB |

**Verdict: the evaluation backend runs on the Jetson today, on the CPU, with a comfortable
memory footprint.** Roughly 10 faces per second of full extraction on one CPU thread is ample
for enrollment and for the stills-based qualification, and is a usable floor for recorded-video
work at a low face-processing cadence.

**Not measured, and therefore not claimed:** any CUDA or TensorRT figure. The PyPI wheel is
CPU-only, so `DNN_BACKEND_CUDA` is unavailable in this build despite appearing in the enum.

Next-stage path for V1-02B1, in order of increasing effort:

1. **CPU as-is.** No work. Run face recognition on a subset of frames — the existing tracker
   already gives persistent person tracks (ADR 0012), so identity needs to be resolved once per
   track and re-checked occasionally, not once per frame.
2. **ONNX Runtime with the CUDA execution provider.** Replace the `cv2.dnn` inference call
   while keeping YuNet's own pre/post-processing; the alignment and the embedding contract are
   unchanged, so stored templates stay valid.
3. **TensorRT**, converting the SFace ONNX to a `.plan` through the existing edge artifact
   convention (`veotrex_edge_agent.model_artifacts`, ADR 0008/0010) and running it in the
   isolated GPU worker. Only worth doing if measurement shows CPU extraction is actually the
   bottleneck — it will not be at a per-track cadence.

Note for any GPU path: the existing GPU worker already opens `/dev/nvmap`, `/dev/nvgpu/igpu0/ctrl`
and `/dev/dri/renderD128`. A face model in that worker needs no new device access.

## 5. Residual risks

1. **Face templates are not encrypted at rest.** Option B of the stage brief was chosen (see
   ADR 0020): real templates are hard-gated to local and development, and staging and
   production stay on `unavailable`. **Anyone with read access to a local development database
   holds recoverable face templates of the people enrolled in it.** Acceptable for a laptop and
   a Jetson holding three consenting adults; not acceptable for Hostinger.
   *Precondition for any staging or production face backend*: versioned AEAD encryption under a
   biometric-specific key — never the Ring credential key — with the ciphertext bound by
   associated data to tenant, staff profile, enrollment image, model identity and template
   version, and decryption in both the recognition path and the package builder.
2. **SFace weight provenance is unresolved upstream** (issue 313). The backend is
   evaluation-only for exactly this reason and cannot be promoted without an upstream answer.
3. **The thresholds are uncalibrated.** They are conservative guesses informed by upstream's
   own verification threshold, not measurements. Section 6 is how they get evidence.
4. **Enrollment photos are still outside the database backup** (carried forward from V1-02A).
5. **A local evaluation database accumulates real biometrics.** Delete the teachers through the
   dashboard when the qualification is finished; deletion revokes every template and removes
   the stored photos.

## 6. Local test procedure

**Local only. Consenting adults only. No child photographs, ever.**

### Install the models once

```bash
./infra/local/fetch-face-eval-models.sh
```

Downloads both weights into the git-ignored `artifacts/models/face/`, verifying size and
SHA-256 before accepting either. A mismatch deletes the file and fails.

### Configure and run

```bash
export VEOTREX_ENVIRONMENT=local
export VEOTREX_STAFF_FACE_BACKEND=opencv_eval
export VEOTREX_STAFF_FACE_MODEL_DIR="$PWD/artifacts/models/face"
export VEOTREX_FACE_EVALUATION_UI=1      # the dashboard panel; server-side, defaults to off
make db-up && make migrate && make api   # and, in another shell, make web
```

If the models are missing or altered, the API refuses to start rather than answering requests
it cannot fulfil. `VEOTREX_ENVIRONMENT=staging` or `production` refuses to start at all with
this backend — that refusal is asserted by `apps/api/tests/test_face_environment_gate.py`.

### Enroll

For each of Chandra, Friend A and Friend B, in **Staff → Add a teacher** (the consent checkbox
is a real gate — confirm consent before ticking it), then add 3–5 photos each:

- one frontal, one turned slightly left, one turned slightly right
- one at a noticeably different distance from the camera
- one with a different but natural expression

Each must show that person alone, face clearly visible and well lit. A photo is refused with a
plain reason if it shows nobody, more than one person, a face too small in the frame, or is too
blurry. A teacher becomes **Ready** at three accepted photos.

### Test

In **Staff → Test recognition**, upload a *new* photo — never one already enrolled — and record
the decision and the score for each case:

| Case | Expected |
|---|---|
| New photo of Chandra | MATCH Chandra |
| New photo of Friend A | MATCH Friend A |
| New photo of Friend B | MATCH Friend B |
| An adult who is not enrolled | UNKNOWN |
| A photo showing two people | Refused: more than one person |
| A photo where the face is small and distant | Refused: face too small |
| A deliberately blurry photo | Refused: too blurry, or UNKNOWN |
| Chandra, after deactivating him | UNKNOWN |
| Chandra, after deleting him | UNKNOWN |

**A false UNKNOWN is a good outcome. A false MATCH is a defect.** If any photo is matched to
the wrong person, stop and record: the score shown, the threshold shown, both people's names,
and what the photos looked like. That is the finding V1-02B1 must not be built on top of.

The panel shows the raw similarity alongside the threshold for every answer. If genuine matches
land only just above 0.45, or a wrong person lands anywhere near it, raise
`VEOTREX_STAFF_RECOGNITION_THRESHOLD` and re-run — the numbers are meant to be tuned by this
exercise, which is the whole point of it.

Nothing about a test photo is kept: no row, no template, no file, and the log records only the
decision and the score, never who was recognised.

### Afterwards

Delete the three teachers through the dashboard. That revokes every template and removes every
stored photo, leaving no biometric material behind.

## 7. What this stage does not do

No production deployment, no change to Hostinger, no Ring interaction, no child recognition, no
video, no tracking, no fall detection, no phone-use detection, and no automatic enrollment from
footage — enrollment remains an explicit operator action with recorded consent.
