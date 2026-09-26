# ADR 0022: Adaptive live inference scheduling

- Status: Accepted (V1-03A)
- Date: 2026-09-25

## Context

The first real Ring qualification (Ring → broker → WebRTC → NVDEC → YOLOX-S TensorRT →
`PersonTracker`) worked end to end and reported 195 frames captured, 76 processed and
117 "dropped". The number read as frame loss, and it was not.

`BackpressureScheduler` held a single-slot, newest-frame-wins buffer. The consumer took a
frame the moment it was free, ran detector → tracker → preview on it, and came back. Every frame
that arrived while it was busy overwrote the one waiting, and every overwrite incremented
`frames_dropped_total`. With a ~91 ms pipeline behind a faster camera, most frames were always
going to be overwritten. All 117 were that sampling; the remaining 2 of 195 were discarded at
shutdown (one delivered but abandoned, one still in the slot). No transport loss was involved.

The audit also found:

- Capture and processing FPS were averaged over windows that included Ring session negotiation,
  so both understated the steady-state rates.
- The consumer cleared its wake-up event *after* checking the slot, so an arrival between the
  two could idle it for up to 50 ms.
- Nothing consumed `LiveFrame.discontinuity`. The tracker's own gap reset clears tracks without
  reporting them, so the pipeline kept them "live" forever and never emitted
  `PERSON_NO_LONGER_VISIBLE` for them.
- A discontinuity flag on a superseded frame was lost with it.
- The runtime published occupancy from the *previous* frame's boxes, so one missed detection
  toggled occupancy 1 → 0 → 1.
- `run_demo_cli` validated the preview settings and ignore regions after starting the GPU
  worker, and returned without stopping it when either was rejected.

## Decision

### Inference is sampled, not exhaustive

A ~16 fps stream and a ~100 ms detector cannot both be honoured. Running YOLOX on every camera
frame was never the goal; running it on a *current* frame at a sustainable cadence is. The
single-slot, newest-frame-wins buffer stays exactly as it was (capacity 1, bounded by
construction, no queue anywhere), and an `AdaptiveInferencePacer` decides *when* the consumer
takes the next frame.

```
Ring / USB capture thread ──► [1-slot latest frame] ──► pacer: due? ──► YOLOX ──► tracker
                                  ▲ overwritten:                              │
                                  │ not due → scheduler skip                  ▼
                                  │ due+busy → backpressure drop     occupancy / events
                                                                     preview (same frame)
```

### The pacer

`InferenceRateConfig(target_fps=6, min_fps=3, max_fps=8, safety_factor=1.25, sample_window=32,
min_samples=5, adjust_interval_seconds=1.0, recovery_step=0.10, hysteresis=0.05)`.

- The next frame is due one period after the previous one was selected.
- The pacer measures per-inference service time (hand-over to next request: detector + tracker
  + preview + publish), which is conservative relative to detector latency alone.
- At most once per second: `desired = clamp(max(1/target, p95 × 1.25), 1/max, 1/min)`.
  Slower → applied immediately. Faster → at most 10 % per adjustment. Changes under 5 % are
  ignored.
- It never schedules above `target_fps`; `max_fps` is the hard ceiling the target is validated
  against. If the detector cannot even sustain `min_fps`, the period holds at `1/min_fps`,
  inference runs back to back, and `inference_scheduler_state` reads `DETECTOR_BELOW_MINIMUM`.
- It is pure (no threads, no clock of its own), so it is tested deterministically under a fake
  clock.
- A discontinuity frame is made due immediately, so a reconnected camera reaches the tracker
  without waiting out a period.

The defaults come from measurement on this Jetson (R39.2.1 engine): detector p95 ~106 ms and
pipeline p95 ~117 ms; 6 fps uses ~70 % of one inference slot, and 8 fps is the most that fits
under the 1.25 safety margin at that cost. The live-demo CLI always paces (`--inference-fps`,
`--inference-min-fps`, `--inference-max-fps`); `LiveDemoRuntime` without an `inference_rate`
stays unpaced, which is what the existing sequence-exact tests rely on.

### Frame accounting, by cause

| Counter | Meaning |
|---|---|
| `camera_frames_captured_total` (= `frames_captured_total`) | every frame the source delivered |
| `inference_frames_selected_total` (= `frames_delivered_total`) | taken for inference |
| `inference_frames_processed_total` | detection + tracking completed |
| `inference_frames_skipped_scheduler_total` | overwritten while inference was not yet due — intentional |
| `inference_frames_dropped_backpressure_total` (= `frames_dropped_total`) | overwritten while inference was due and the detector busy — true pipeline loss |
| `inference_frames_discarded_on_stop_total` | still in the slot at shutdown |
| `source_frames_dropped_total` | lost before capture (geometry rejects, unmappable frames); `null` when the source cannot measure it |

Always: `captured = selected + skipped + backpressure + discarded_on_stop + (0 or 1 waiting)`,
and `selected − processed ≤ 1`. **`frames_dropped_total` changed meaning**: it now counts only
backpressure. Unpaced, every supersession is backpressure, so its value there is unchanged.

Rates: `camera_capture_fps` and `effective_inference_fps` (also `processing_fps`) are measured
from the first to the latest event, so session negotiation no longer dilutes them.
`preview_fps` is measured the same way. `inference_handoff_age_ms` is how long the selected
frame had waited in the slot; it stays at roughly one camera interval when there is no backlog.
Every latency series is a fixed-capacity reservoir (scheduler 512, pipeline 4096, preview 512,
pacer window 32).

### Tracking between inferences

`PersonTracker` has no prediction-only update, and calling `update` with no detections would
record a *miss*. So the tracker runs only on frames the detector ran on, each with its own
capture-side timestamp; skipped frames neither compress time nor count as misses. Its
tolerances are in seconds (`max_lost_seconds`, `max_timestamp_gap_seconds`), so they are
independent of cadence and the defaults are unchanged. No box is predicted, interpolated or
synthesised; the preview draws only boxes detected on the frame it shows.

Occupancy is the number of tracks reported as appeared and not yet as gone: the same lifecycle as
`PERSON_APPEARED_IN_VIEW` / `PERSON_NO_LONGER_VISIBLE`. A track briefly lost inside the
tolerance still counts, so one missed detection changes neither the count nor the timeline. A
real exit is still reported once the tolerance expires (default 2 s).

### Discontinuity

`PersonTracker.update` gained one additive keyword, `source_discontinuity` (default `False`),
which clears the stream exactly as an over-long gap does: same metrics, and track ids continue.
It is ignored on a stream's first frame. The pipeline passes the frame's flag through, and
whenever the tracker reports a discontinuity (source-signalled, gap or geometry) it ends every
live track with the new `TrackEndReason.DISCONTINUITY` before applying the frame. That emits
`PERSON_NO_LONGER_VISIBLE` rather than silently joining pre-reconnect state to post-reconnect
state. The scheduler carries a superseded frame's flag onto the frame that replaced it (a new
frame object over the same pixels, with the timestamp untouched).

### Preview

Unchanged in structure: encoded on the inference thread, on inference frames only, under its own
throttle. That keeps the V1-DEMO-01R1 invariant that a box is drawn on the frame it was detected
in. Preview rate is therefore `min(--preview-fps, inference rate)`. Its throttle is independent
of the pacer, and preview cost is part of the service time the pacer adapts to.

## Consequences

- Inference no longer runs the GPU flat out. At the default rate the GPU worker has headroom,
  which a second camera will need.
- Dashboard rows now separate intentional sampling from loss. The old "Frames dropped" row is
  gone; its successor, "Backpressure drops", should sit near zero.
- A reconnect now produces `PERSON_NO_LONGER_VISIBLE` for every live track and a new track id
  afterwards. That is truthful: nothing links the two sides of a gap.
- Transport loss inside the Ring decode worker (its bounded appsink, frames in flight) is not
  reported across the socket, so `source_frames_dropped_total` is a floor.

## Measured (single camera, this Jetson - not a capacity claim)

Synthetic, 30 s: 16 fps generated 1280x720 frames, a fake detector sleeping 90-110 ms, preview
on, default rate.

| | |
|---|---|
| capture / inference / preview fps | 15.8 / 5.95 / 5.96 |
| scheduler skips / backpressure drops | 295 / 0 |
| queue depth max | 1 |
| handoff age p50 / p95 | 30 / 60 ms (one source interval is 62.5 ms) |
| detector p50 / p95, pipeline p50 / p95 | 99.6 / 109.1 ms, 113.3 / 123.9 ms |
| RSS start / peak / end | 74 / 101 / 101 MB; no threads or child processes left |

One real Ring camera, 30 s, YOLOX-S FP16 on the R39.2.1 engine, default rate, preview on:

| | Before (greedy) | After (paced) |
|---|---|---|
| capture fps | "15.9" (diluted by negotiation) | 24.1 |
| inference fps | "6.2" | 5.68 (scheduled 6.0, `AT_TARGET`) |
| frames captured / processed | 195 / 76 | 639 / 151 |
| reported as dropped | 117 (all sampling) | 2 backpressure, 485 scheduler skips |
| detector p50 / p95 | 84.9 / 106.1 ms | 100.5 / 119.9 ms |
| tracker p50 / p95 | 0.8 / 1.0 ms | 1.2 / 1.6 ms |
| pipeline p50 / p95 | 91.3 / 116.7 ms | 109.8 / 130.8 ms |
| reconnects | 0 | 0 |

The GPU load (GR3D) averaged 33 % (peak 60 %) with the system service's own worker also running,
and temperatures stayed at 52-54 C.

This is one Ring camera on one Jetson Orin Nano. It says nothing yet about 24 cameras, and no
multi-camera capacity is claimed. In that session a stationary, low-confidence detection
(0.07-0.32) beside the adult was confirmed as a second track and held for the whole session by
the tracker's low-score recovery, so occupancy read 2 for one person. That is detector/scene
behaviour, not scheduling. It is recorded as a known issue; the existing `--ignore-region`
mitigation applies once an operator has identified the object.
