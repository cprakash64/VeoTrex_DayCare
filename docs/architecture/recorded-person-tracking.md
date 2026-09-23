# Recorded-video person detection and tracking (V1-02B1A)

The substrate every later temporal feature is built on. It turns a local recording into stable,
anonymous person tracks and nothing more.

```
video file
  -> RecordedVideoSource      streaming frames + media-timeline timestamps
  -> PersonDetector           boxes for this frame only
  -> BoundingBoxValidator     clamp what is recoverable, reject what is not
  -> PersonTracker            temporal continuity, stable ids within one run
  -> TrackObservation         per-frame record
  -> TrackSummary             per-track record, emitted once the track ends
  -> tracks.ndjson            versioned, machine-readable
  -> annotated.mp4            optional, local evaluation only
```

## The four words that must not be confused

**DETECTION != TRACKING != IDENTITY != EVENT.** Each is a different claim with different
evidence behind it, and conflating any two of them is how a monitoring product starts making
assertions it cannot support.

| Concept | What it claims | What it does not claim |
|---|---|---|
| **Detection** | A person-shaped thing is at these coordinates in this frame | That it is the same person as any other frame |
| **Track** | These observations across time are one continuous person | Who that person is |
| **Identity** | This track is an enrolled, consenting staff member | Anything about anyone not enrolled |
| **Event** | Something meaningful happened in the daycare | — derived later from temporal evidence, never from a single track fact |

A detector box is an *observation*. A track is *temporal continuity*. Teacher identity is an
*optional annotation* supported only for enrolled, consenting staff. An event is *business
logic* derived later. This stage produces the first two and defines the boundary for the
third; it produces no events at all.

## Track lifecycle is deliberately not daycare vocabulary

`TRACK_STARTED`, `TRACK_ACTIVE`, `TRACK_ENDED` are facts about the tracker, not about a room.

A track starting does **not** mean somebody entered. A track ending does **not** mean somebody
left — far more often it means they were occluded, turned away, walked behind furniture, or the
detector missed them for longer than the tolerance. Calling those `TEACHER_ENTERED` and
`TEACHER_EXITED` would bake an unsupported inference into the data model, and every consumer
downstream would inherit it.

Entry and exit require a configured doorway or line-crossing semantic that this stage does not
have. They arrive in a later temporal stage, derived from evidence, and they will be separate
records rather than a renaming of these.

`TrackEndReason` keeps the two honest cases apart:

- `ABSENT` — not matched for longer than the configured tolerance.
- `STREAM_ENDED` — the video ran out while the track was still live. Not a disappearance, and
  a later stage must never count it as one.

## Tracking is anonymous by construction

Nothing in this pipeline knows what a face is. Association is motion and geometry only
(ByteTrack-style IoU with a Kalman predictor — ADR 0012), so:

- children are tracked exactly as well as adults, with no biometric processing whatsoever;
- an unenrolled adult is tracked exactly as well as an enrolled one;
- a person whose face is never visible to the camera is tracked normally.

This ordering is the point:

```
person detector -> tracker -> stable anonymous track   (correct)
face recognition -> identity -> track                  (never)
```

Recognition must never be responsible for frame-to-frame tracking. If it were, anyone the
system could not identify would become untrackable — which is every child and most adults.

## The recognition boundary (for V1-02B1B)

`TrackIdentityObservation` is the shape a later stage will attach to an **already-existing**
track. Nothing in this stage produces one. It cannot create or influence a track; it carries a
`track_id` that must already exist.

It enforces two rules in its constructor rather than by convention:

- a `MATCH` must carry a staff profile;
- an `UNKNOWN` must **not** carry one — an identity that was refused does not travel with the
  name it refused to give.

**UNKNOWN does not mean "child".** It means "not identified", and most UNKNOWN tracks will be
adults the system has no reason to name. No consumer may infer one from the other.

### Intended next-stage behaviour

V1-02B1B should recognise *sparsely and cumulatively*, not per frame:

1. wait until a track is established, not on its first confirmed frame;
2. sample a bounded number of high-quality face opportunities from that track;
3. recognise periodically rather than continuously;
4. accumulate evidence across several observations before assigning a persistent identity;
5. remain UNKNOWN when the evidence is insufficient, and be willing to fall back to it;
6. never attempt to identify a child;
7. never let one noisy frame permanently label a track.

The V1-02B0 thresholds (cosine 0.45, second-best margin 0.06) are **evaluation values** and are
not to be tuned by tracking work. The required three-real-adult manual recognition
qualification is still **PENDING**, so no recognition claim rests on this stage.

## Video input contract

| Property | Behaviour |
|---|---|
| Containers | `.mp4`, `.m4v`, `.mov` — a small allow-list, not "whatever OpenCV opens" |
| Input | Local filesystem paths only. **No URL is accepted anywhere in this stage** |
| Timestamps | The media timeline (`CAP_PROP_POS_MSEC` read immediately *after* a successful read), falling back to frame-rate timestamps when the timeline is absent, non-finite or stuck |
| Speed independence | Processing faster or slower than source FPS changes no timestamp — nothing consults the wall clock |
| Sampling | `--sample-every N` skips frames; the remaining timestamps stay on the source timeline |
| Memory | Exactly one decoded frame is live at a time |
| Refusals | missing, empty, symlink, directory, unsupported container, undecodable, zero-frame, dimensions outside bounds |

The OpenCV timestamp semantics were measured on this repository's own demo file rather than
assumed: read *before* `read()` the position lags by one frame, which would silently shift the
whole time axis. Corrupt frames mid-file are skipped with a bounded retry; exhausting the
retries is treated as the end of the stream, and trailing end-of-file retries are **not**
counted as dropped frames.

## Detector

`PersonDetector` is a protocol. Two implementations ship:

- **`FakePersonDetector`** — deterministic, script-driven, no model. CI covers the entire
  pipeline with it.
- **`YoloxPersonDetector`** — the existing YOLOX-S FP16 TensorRT engine in the isolated GPU
  worker (ADR 0008/0010/0011), wrapped so the pipeline depends on a protocol rather than CUDA.

YOLOX-S is **provisionally licensed** (ADR 0009): Apache-2.0 code, with no separate terms
stated upstream for the pretrained artifact, so redistribution remains subject to licence
review. It therefore refuses to construct outside `local` / `development` / `test` / `ci`,
exactly like the V1-02B0 face backend. The engine is a locally built, git-ignored artifact the
worker resolves and SHA-256-verifies itself. **Nothing downloads a model**, and a test greps
the package to keep it that way.

## Output

`tracks.ndjson`, one JSON object per line, each carrying `schema_version`:

| Record | Contents |
|---|---|
| `header` | run id, video **filename only**, detector id/version, source geometry, `identity_annotation: "none"` |
| `observation` | track id, frame index, timestamp, bbox, confidence, track state, lifecycle |
| `track_summary` | track id, first/last seen, duration, observation count, max confidence, end reason |
| `footer` | record count and the run's final metrics |

Written 0600, created exclusively — a second run into the same directory is refused rather than
silently replacing the first run's evidence. A failed write removes the partial file.

**No face embedding, biometric template, face crop or frame image ever appears in the output.**
`assert_no_biometric_material` enforces this at write time by rejecting both forbidden key
names at any depth and long opaque strings, and a test proves the guard fires.

`annotated.mp4` is optional (`--annotate`), local-evaluation only, drawn on a copy so the input
is never modified, and labels boxes with **track ids only** — no names, because this stage has
no identities and a name on a box is exactly what would make an anonymous track look like a
named person.

## Operator usage

```bash
veotrex-edge track-recording \
  --input /path/to/clip.mp4 \
  --output-dir /path/to/run \
  --detector yolox \
  --environment local
```

`--detector none` runs the whole pipeline with a detector that finds nobody — useful for
checking that a file decodes and its timestamps are sane before spending GPU time, and it works
on a machine with no GPU at all.

## Resource bounds

Streaming frames, one live at a time; per-track history capped by the tracker; fixed-capacity
latency reservoirs; completed tracks emitted and dropped rather than accumulated; bounded
detections per frame; `VideoCapture` and the GPU worker released on every exit path including
mid-iteration failure. Processing a two-hour recording costs what processing two minutes costs.

## Known limitation: association across gaps

Recovery of a track across missed frames is IoU-based, so it depends on the person's predicted
box still overlapping their real one. Measured on the synthetic fixture: a two-frame gap is
bridged while motion is at or below ~13% of box width per frame, and is **not** bridged at 20%,
where the track splits and the person is issued a new id.

This is a property of IoU association rather than a defect, and it is asserted as a test so any
change to it is visible. It is also the main reason a track id means *continuity within one
run* and must never be treated as an identity.
