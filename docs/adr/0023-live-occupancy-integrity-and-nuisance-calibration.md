# ADR 0023: Live occupancy integrity and camera nuisance calibration

- Status: Accepted (V1-03B), provisional. This is not a daycare or child qualification.
- Date: 2026-09-25

## Context

The V1-03A Ring qualification showed one adult and an occupancy of two. The second track was a
static box (~73×305 px at 1280×720, centre spread ~0.4 px) scoring 0.07–0.32 that lasted the
whole session. Before any ratio, supervision or compliance logic is built on top of occupancy,
it has to be clear why that happened and how it is contained without losing real people.

### How the nuisance became an occupant

The code was traced and then reproduced with scripted scores:

| | |
|---|---|
| high / low / new-track thresholds | `score >= 0.30` / `0.05 <= score < 0.30` / `>= 0.30` (ADR 0012; detector emits `>= 0.05`, NMS 0.45) |
| birth | only an unmatched **high** detection creates a TENTATIVE track |
| confirmation | 2 observations; a TENTATIVE track associates only with **high** detections |
| low-score association | only for tracks that are currently CONFIRMED (IoU cost ≤ 0.50); LOST tracks recover only on a high detection |
| occupancy (before) | every track reported as confirmed and not yet ended: CONFIRMED, plus LOST within 2.0 s; never TENTATIVE |

Two consecutive frames at 0.31 and 0.32 created and confirmed the track, and the documented
low-score association then kept it CONFIRMED on every 0.07–0.29 frame. It never dropped below
0.05, so it was never even LOST. **This is ADR 0012's documented policy working as written, not
a birth bug.** Low-score detections cannot create or confirm a track, which the new tests pin.

The audit did find one real tracker bug: `_observe` set every matched track to CONFIRMED, so a
TENTATIVE track was confirmed on its second observation whatever `confirmation_observations`
said. That was harmless at the default of 2, so it was not the cause, and it is fixed.

## Decision

### Four separate concepts

- **Detection:** the model emitted a person candidate for one frame.
- **Track:** temporal association (ADR 0012), deliberately high-recall.
- **Occupancy eligibility:** whether a confirmed track's evidence is strong enough to change the
  head count. This is new, in `live/occupancy.py`.
- **Nuisance calibration:** an operator's explicit exclusion of a known fixed artifact
  (`recorded/regions.py`). It is never automatic.

### No threshold was changed

The high, low, new-track, confirmation and NMS values are unchanged. The only evidence against
them is one static object in one room. Children, distant and partly occluded people legitimately
score in the same 0.07–0.32 range, so raising any global threshold would hide them silently.

### Occupancy evidence

A confirmed track is `OCCUPANCY_CANDIDATE` until at least **3 of its last 10** observations
were high-score by the tracker's *own* 0.30 threshold (the 2 pre-confirmation observations count,
since the tracker only accepts high scores for those). It is then `OCCUPANCY_VALIDATED`, and
stays validated for the life of the track, so one bad stretch cannot make the count flap. The
track still ends through the tracker's 2 s tolerance.

- Only validated tracks change occupancy or emit `PERSON_APPEARED_IN_VIEW` /
  `PERSON_NO_LONGER_VISIBLE`.
- Candidates are drawn (thin grey "Candidate N"), counted separately ("Candidate person tracks:
  N (shown, not counted)"), explained in diagnostics, and never discarded. They are not
  "not a person" and must never feed compliance logic.
- **Motion is not an input.** A motionless person validates exactly as fast as a moving one,
  and a moving low-confidence box is not validated for moving. Staticness proves nothing: people
  sit, sleep and stand still.
- Per-track state is bounded: a 10-entry window, the last 32 scores, running moments, at most
  512 live tracks, and diagnostics for the last 16 ended tracks.
- Metrics: `occupancy_candidate_tracks_total`, `occupancy_candidate_to_validated_total`,
  `occupancy_candidates_ended_unvalidated_total`, `occupancy_validated_tracks_ended_total`, and
  the gauges `occupancy_validated_tracks`, `occupancy_candidate_tracks`,
  `nuisance_review_candidates`.

### Nuisance diagnostics (no pixels)

Per track, the diagnostics record:

- status, first and last time, observations and high observations;
- confidence min / max / mean and recent p50 / p95;
- normalised box envelope;
- normalised centre mean and spread (diagnostic only);
- mean normalised size and aspect.

A candidate alive for at least 5 s over at least 10 observations is labelled
`PERSISTENT_LOW_CONFIDENCE_CANDIDATE` and gets a `suggested_ignore_region`: its envelope plus a
10 % margin, with containment 0.8, `requires_operator_review: true` and `applied: false`. The
suggestion is only ever printed. It is applied only if an operator passes it back as
`--ignore-region`.

### Ignore regions: hardened and visible

- Applied before the tracker, as before.
- Rule: a detection is ignored if at least `min_containment` (default 0.8) of it lies inside one
  region.
- At most 8 regions; at most 50 % of the frame each and 75 % in total. Whole-frame and
  near-whole-frame exclusions fail closed.
- Sides must be at least 0.5 % of the frame.
- NaN, Inf and bool coordinates are refused, as are inverted or out-of-frame regions.
- Labels are 40 characters or fewer, from `[A-Za-z0-9 _.()/:#-]`, so they can never carry markup.
- A containment below 0.5 is still allowed, as before, but is flagged on the CLI, the status and
  the dashboard, because it can suppress a person standing in front of the region.
- Regions, labels, the rule and the suppressed count appear on `/api/state` (`calibration`) and
  in the dashboard's "Camera calibration" card, which is rendered with `textContent` only.
- Regions are outlined and numbered on the preview; labels are never drawn into the picture.

## Result on the real camera (one run, 30 s, no ignore region)

The nuisance reproduced. Normalised envelope `[0.708, 0.263, 0.773, 0.694]`; confidence 0.078–0.366,
mean 0.217; 145 observations, 18 of them high (12 %); centre spread 0.0004 / 0.001; present
for 25.4 s.

**The evidence rule did not contain it.** It reached 3 high observations within its first 10 and
was validated 0.9 s after appearing, so occupancy still read 2. The adult (0.87–0.91) was
validated 171 ms after appearing and kept one track ID throughout. Because the review label is
defined only for candidates, it did not fire on this validated track.

The rule is left as tested rather than re-tuned from this one observation. Containment for this
camera is an operator-confirmed ignore region (see the stage report), which must be reviewed
visually first: the proposed strip would not suppress an adult-sized box (≤ 13 % containment),
but it would suppress a much smaller person box lying entirely inside it.

## Consequences and limits

- Occupancy now needs slightly more evidence: a clearly visible person is counted about one
  inference frame (~170 ms) later than before.
- A real person whose detections stay mostly below 0.30 may remain a candidate. They are shown,
  not hidden, but they are not counted. That is the explicit uncertainty this design chooses
  over silent loss; it needs daycare-representative calibration before any ratio logic relies on
  it.
- A nuisance whose score crosses 0.30 often enough is validated, as happened here. Evidence
  alone cannot separate it from a person; only explicit, reviewed calibration can.
- Nothing here qualifies children, daycare occupancy, ratios or compliance.
