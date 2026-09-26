"""Adaptive live inference scheduling (V1-03A).

Two layers, tested differently on purpose:

* ``AdaptiveInferencePacer`` and the scheduler's frame classification are pure decisions and are
  driven by a fake clock, so every assertion about rates and counters is exact.
* The runtime is exercised end to end with ``FakeLiveSource`` at 16 fps and a detector that
  sleeps ~100 ms, which is the rate mismatch the real Jetson sees. Those tests assert bounds
  (never exact counts) so they do not depend on how busy the machine is.

Every frame is a small generated NumPy array. No camera, no GPU, no model, no recording.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.live import (
    AdaptiveInferencePacer,
    BackpressureScheduler,
    DemoEventKind,
    FakeLiveSource,
    InferenceRateConfig,
    LiveDemoRuntime,
    LiveFrame,
    PreviewConfig,
    PreviewRenderer,
    SourceKind,
)
from veotrex_edge_agent.live import cli as live_cli
from veotrex_edge_agent.live.scheduler import SCHEDULER_SAMPLE_CAPACITY
from veotrex_edge_agent.live.server import DemoServer
from veotrex_edge_agent.recorded.model import DetectedPerson, TrackEndReason
from veotrex_edge_agent.recorded.pipeline import RecordedTrackingPipeline
from veotrex_edge_agent.tracking import TrackingConfig

WIDTH, HEIGHT = 320, 240
SOURCE_FPS = 16.0
CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
LIVE_THREAD_PREFIXES = ("veotrex-live", "veotrex-demo-http")


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def live_threads() -> list[str]:
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(LIVE_THREAD_PREFIXES) and thread.is_alive()
    )


def wait_for_no_live_threads(timeout: float = 3.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = live_threads()
        if not remaining:
            return []
        time.sleep(0.02)
    return live_threads()


def person_box(index: int) -> tuple[float, float, float, float]:
    """One adult-sized box walking slowly right: 2 px per camera frame at 16 fps."""
    x = 20.0 + index * 2.0
    return (x, 50.0, x + 60.0, 200.0)


class LatencyDetector:
    """A detector that costs real time, like YOLOX does, and reports what it was given.

    ``present`` decides, per frame index, whether the walking person is in view.
    """

    model_id = "latency-fake"
    model_version = "1"

    def __init__(
        self,
        latency: float | Callable[[int], float] = 0.1,
        *,
        present: Callable[[int], bool] = lambda _index: True,
        fail_on_call: int | None = None,
    ) -> None:
        self._latency = latency
        self._present = present
        self._fail_on_call = fail_on_call
        self.seen: list[tuple[int, float]] = []
        self.entered = threading.Event()

    def detect(self, image: Any, *, frame_index: int, timestamp_ms: float) -> list[DetectedPerson]:
        self.seen.append((frame_index, timestamp_ms))
        self.entered.set()
        if self._fail_on_call is not None and len(self.seen) >= self._fail_on_call:
            raise RuntimeError("synthetic detector failure")
        latency = self._latency(frame_index) if callable(self._latency) else self._latency
        time.sleep(latency)
        if not self._present(frame_index):
            return []
        return [DetectedPerson(person_box(frame_index), 0.9, frame_index, timestamp_ms)]


class DiscontinuousSource(FakeLiveSource):
    """A synthetic source whose feed is interrupted before ``discontinuity_at``."""

    def __init__(self, *, discontinuity_at: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._discontinuity_at = discontinuity_at

    def frames(self) -> Iterator[LiveFrame]:
        for frame in super().frames():
            if frame.frame_index == self._discontinuity_at:
                frame = dataclasses.replace(frame, discontinuity=True)
            yield frame


def sixteen_fps_source(frames: int, **kwargs: Any) -> FakeLiveSource:
    return FakeLiveSource(
        frame_count=frames,
        width=WIDTH,
        height=HEIGHT,
        fps=SOURCE_FPS,
        interval_seconds=1.0 / SOURCE_FPS,
        **kwargs,
    )


def frame(index: int, *, timestamp_ms: float | None = None, discontinuity: bool = False) -> Any:
    return LiveFrame(
        kind=SourceKind.SYNTHETIC_TEST,
        source_id="synthetic-test",
        frame_index=index,
        timestamp_ms=index * 62.5 if timestamp_ms is None else timestamp_ms,
        monotonic_ns=index,
        width=WIDTH,
        height=HEIGHT,
        image=np.zeros((HEIGHT, WIDTH, 3), np.uint8),
        discontinuity=discontinuity,
    )


class IdleSource:
    """A source the white-box scheduler tests never start; frames are admitted by hand."""

    kind = SourceKind.SYNTHETIC_TEST
    source_id = "idle"
    health = "RUNNING"

    def frames(self) -> Iterator[LiveFrame]:  # pragma: no cover - never iterated
        return iter(())

    def close(self) -> None:
        return None


def events(runtime: LiveDemoRuntime, kind: DemoEventKind) -> list[dict[str, Any]]:
    return [event for event in reversed(runtime.timeline.recent(200)) if event["kind"] == kind]


# ================================================================ pacer: fake-clock control
def test_the_first_frame_is_due_immediately_at_the_target_rate() -> None:
    pacer = AdaptiveInferencePacer(InferenceRateConfig(target_fps=6.0))
    assert pacer.due_in(0.0) == 0.0
    assert pacer.scheduled_fps == pytest.approx(6.0)
    pacer.on_selected(10.0)
    assert pacer.due_in(10.0) == pytest.approx(1 / 6)
    assert pacer.due_in(10.0 + 1 / 6) == 0.0
    assert pacer.state == "WARMING_UP"


def test_the_schedule_never_leaves_the_configured_band() -> None:
    config = InferenceRateConfig(target_fps=6.0, min_fps=3.0, max_fps=8.0)
    pacer = AdaptiveInferencePacer(config)
    clock = FakeClock()
    rng = np.random.default_rng(7)
    for _ in range(2000):
        clock.advance(0.2)
        pacer.observe(float(rng.choice([0.001, 0.05, 0.1, 0.2, 0.4, 2.0])), clock())
        assert config.shortest_period - 1e-12 <= pacer.period <= config.longest_period + 1e-12
        assert config.min_fps - 1e-9 <= pacer.scheduled_fps <= config.target_fps + 1e-9


def run_trace(service: list[float]) -> list[float]:
    pacer = AdaptiveInferencePacer(InferenceRateConfig())
    clock = FakeClock()
    trace = []
    for value in service:
        clock.advance(0.25)
        pacer.observe(value, clock())
        trace.append(pacer.period)
    return trace


def test_adaptation_is_deterministic_under_a_fake_clock() -> None:
    service = [0.09, 0.1, 0.11, 0.3, 0.25, 0.08, 0.07, 0.12] * 20
    assert run_trace(service) == run_trace(service)


def test_higher_detector_latency_lowers_the_rate_immediately() -> None:
    pacer = AdaptiveInferencePacer(InferenceRateConfig(target_fps=6.0, min_fps=3.0))
    clock = FakeClock()
    for _ in range(5):
        clock.advance(0.2)
        pacer.observe(0.2, clock())
    # p95 200 ms x 1.25 safety = 250 ms: 4 fps, in one step, the moment it is measured.
    assert pacer.period == pytest.approx(0.25)
    assert pacer.scheduled_fps == pytest.approx(4.0)
    assert pacer.state == "ADAPTED"


def test_lower_latency_recovers_gradually_and_never_oscillates() -> None:
    config = InferenceRateConfig(target_fps=6.0, min_fps=3.0, max_fps=8.0)
    pacer = AdaptiveInferencePacer(config)
    clock = FakeClock()
    for _ in range(40):
        clock.advance(0.3)
        pacer.observe(0.24, clock())
    assert pacer.period == pytest.approx(0.3)
    recovering = [pacer.period]
    for _ in range(200):
        clock.advance(0.2)
        pacer.observe(0.05, clock())
        recovering.append(pacer.period)
    steps = [later / earlier for earlier, later in pairwise(recovering) if later != earlier]
    assert steps, "the rate must recover once the load is gone"
    # Never faster than one bounded step at a time, and never back up again: no oscillation.
    assert all(1 - config.recovery_step - 1e-9 <= ratio < 1.0 for ratio in steps), steps
    assert recovering[-1] == pytest.approx(config.target_period)
    assert min(recovering) >= config.target_period - 1e-12


def test_the_rate_changes_at_most_once_per_adjust_interval() -> None:
    config = InferenceRateConfig(adjust_interval_seconds=1.0)
    pacer = AdaptiveInferencePacer(config)
    clock = FakeClock()
    for _ in range(5):
        clock.advance(0.05)
        pacer.observe(0.3, clock())
    adjusted_at = pacer.adjustments_total
    for _ in range(15):  # 0.75 s later: still inside the interval
        clock.advance(0.05)
        pacer.observe(0.01, clock())
    assert pacer.adjustments_total == adjusted_at


def test_a_single_latency_spike_does_not_move_the_rate() -> None:
    pacer = AdaptiveInferencePacer(InferenceRateConfig())
    clock = FakeClock()
    for index in range(64):
        clock.advance(0.2)
        pacer.observe(1.5 if index == 40 else 0.1, clock())
    assert pacer.scheduled_fps == pytest.approx(6.0)
    assert pacer.state == "AT_TARGET"


def test_small_changes_are_absorbed_by_hysteresis() -> None:
    config = InferenceRateConfig(target_fps=6.0, hysteresis=0.05)
    pacer = AdaptiveInferencePacer(config)
    clock = FakeClock()
    # 1/6 s = 166.7 ms. 0.138 x 1.25 = 172.5 ms: under 5 % above the current period.
    for _ in range(10):
        clock.advance(1.0)
        pacer.observe(0.138, clock())
    assert pacer.period == pytest.approx(config.target_period)
    assert pacer.adjustments_total == 0


def test_a_detector_slower_than_the_minimum_holds_at_the_minimum_and_says_so() -> None:
    pacer = AdaptiveInferencePacer(InferenceRateConfig(min_fps=3.0))
    clock = FakeClock()
    for _ in range(10):
        clock.advance(1.0)
        pacer.observe(0.5, clock())
    assert pacer.scheduled_fps == pytest.approx(3.0)
    assert pacer.state == "DETECTOR_BELOW_MINIMUM"


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_fps": 7.0, "target_fps": 6.0},
        {"target_fps": 9.0, "max_fps": 8.0},
        {"min_fps": 0.1},
        {"max_fps": 60.0},
        {"safety_factor": 0.5},
        {"sample_window": 2},
        {"min_samples": 0},
        {"recovery_step": 0.0},
        {"hysteresis": 0.6},
        {"adjust_interval_seconds": 0.0},
    ],
)
def test_an_invalid_rate_configuration_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        InferenceRateConfig(**overrides)


def test_non_finite_service_times_are_ignored() -> None:
    pacer = AdaptiveInferencePacer(InferenceRateConfig(min_samples=1))
    pacer.observe(float("nan"), 1.0)
    pacer.observe(-1.0, 2.0)
    assert pacer.service_p95_seconds is None


# ========================================== scheduler: classification under a fake clock
def paced_scheduler(target_fps: float = 5.0) -> tuple[BackpressureScheduler, FakeClock, Any]:
    clock = FakeClock(0.0)
    pacer = AdaptiveInferencePacer(
        InferenceRateConfig(target_fps=target_fps, min_fps=1.0, max_fps=target_fps)
    )
    scheduler = BackpressureScheduler(IdleSource(), pacer=pacer, clock=clock)  # type: ignore[arg-type]
    return scheduler, clock, pacer


def test_scheduler_skips_and_backpressure_drops_are_counted_separately() -> None:
    scheduler, clock, pacer = paced_scheduler(target_fps=5.0)  # period 200 ms
    pacer.on_selected(0.0)
    scheduler._busy = True  # inference on the frame selected at t=0 is in progress
    for at, index in ((0.05, 0), (0.10, 1), (0.15, 2)):
        clock.now = at
        scheduler._admit(frame(index))
    # Superseded before inference was due: sampling decisions, not failures.
    assert scheduler.metrics.frames_skipped_scheduler_total == 2
    assert scheduler.metrics.frames_dropped_total == 0
    clock.now = 0.25  # due since t=0.2, and the detector is still busy
    scheduler._admit(frame(3))
    assert scheduler.metrics.frames_dropped_total == 1
    scheduler._busy = False
    clock.now = 0.26  # due and idle: the consumer is about to take the newest; not a loss
    scheduler._admit(frame(4))
    assert scheduler.metrics.frames_skipped_scheduler_total == 3
    assert scheduler.metrics.frames_dropped_total == 1
    snapshot = scheduler.snapshot()
    assert snapshot["inference_frames_skipped_scheduler_total"] == 3
    assert snapshot["inference_frames_dropped_backpressure_total"] == 1
    assert snapshot["frames_dropped_total"] == 1, "legacy name now means true pipeline loss"
    assert snapshot["inference_queue_depth_max"] == 1
    assert snapshot["inference_queue_capacity"] == 1


def test_without_a_pacer_every_supersession_is_backpressure() -> None:
    clock = FakeClock(0.0)
    scheduler = BackpressureScheduler(IdleSource(), clock=clock)  # type: ignore[arg-type]
    for index in range(5):
        clock.advance(0.01)
        scheduler._admit(frame(index))
    assert scheduler.metrics.frames_dropped_total == 4
    assert scheduler.metrics.frames_skipped_scheduler_total == 0
    assert scheduler.snapshot()["inference_pacing"] == "unpaced"


def test_a_superseded_discontinuity_is_carried_to_the_next_frame_without_copying() -> None:
    scheduler, clock, _ = paced_scheduler()
    scheduler._admit(frame(0, discontinuity=True))
    replacement = frame(1)
    clock.advance(0.01)
    scheduler._admit(replacement)
    head, _ = scheduler._buffer[0]
    assert head.frame_index == 1
    assert head.discontinuity is True, "the reconnect must reach the tracker"
    assert head.image is replacement.image, "no pixel copy to carry a flag"
    assert head.timestamp_ms == replacement.timestamp_ms, "timestamps are never rewritten"


def test_a_discontinuity_makes_the_next_frame_due_immediately() -> None:
    scheduler, clock, pacer = paced_scheduler()
    pacer.on_selected(0.0)
    clock.now = 0.01
    assert pacer.due_in(clock()) > 0
    scheduler._admit(frame(0, discontinuity=True))
    assert pacer.due_in(clock()) == 0.0
    assert scheduler.metrics.source_discontinuities_total == 1


def test_frames_left_in_the_slot_at_stop_are_counted_as_discarded() -> None:
    scheduler, clock, _ = paced_scheduler()
    scheduler._admit(frame(0))
    scheduler.stop()
    assert scheduler.metrics.frames_discarded_on_stop_total == 1
    assert scheduler.depth == 0
    scheduler.stop()
    assert scheduler.metrics.frames_discarded_on_stop_total == 1, "stop is idempotent"


def test_scheduler_and_pacer_memory_is_bounded() -> None:
    scheduler, clock, pacer = paced_scheduler()
    for index in range(5000):
        clock.advance(0.01)
        scheduler._admit(frame(index))
        scheduler.metrics.service_ms.add(1.0)
        scheduler.metrics.handoff_age_ms.add(1.0)
        pacer.observe(0.1, clock())
    assert scheduler.depth <= 1
    assert scheduler.metrics.service_ms.retained_count == SCHEDULER_SAMPLE_CAPACITY
    assert scheduler.metrics.handoff_age_ms.retained_count == SCHEDULER_SAMPLE_CAPACITY
    assert len(pacer._samples) == pacer.config.sample_window
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0))
    for _ in range(5000):
        renderer.encode_ms.add(1.0)
    assert renderer.encode_ms.retained_count <= 512


# ============================== pipeline: time, identity and continuity across sampling gaps
def run_pipeline(frames: list[Any], present: Callable[[int], bool] = lambda _i: True) -> list[Any]:
    detector = LatencyDetector(0.0, present=present)
    pipeline = RecordedTrackingPipeline(detector, tracking_config=CONFIG)
    return list(pipeline.process(frames, run_id="sampling-test"))


def test_skipped_frames_never_compress_time() -> None:
    """A person seen at t=1.0 s and next at t=1.2 s is 200 ms apart, however many camera
    frames arrived in between and were not detected on."""
    selected = [frame(16, timestamp_ms=1000.0), frame(19, timestamp_ms=1200.0)]
    records = run_pipeline(selected)
    stamps = [r.observation.timestamp_ms for r in records if r.observation is not None]
    assert stamps == [1200.0]  # confirmed on the second observation, at its own time
    summary = next(r.summary for r in records if r.summary is not None)
    assert summary.first_seen_ms == 1200.0
    assert summary.end_reason is TrackEndReason.STREAM_ENDED


def test_track_ids_survive_intentional_sampling_gaps() -> None:
    """Every third camera frame is detected on: one person, one id, one start."""
    selected = [frame(index) for index in range(0, 96, 3)]
    records = run_pipeline(selected)
    observations = [r.observation for r in records if r.observation is not None]
    assert {o.track_id for o in observations} == {1}
    started = [o for o in observations if o.lifecycle.value == "TRACK_STARTED"]
    assert len(started) == 1
    ends = [r.summary for r in records if r.summary is not None]
    assert [s.end_reason for s in ends] == [TrackEndReason.STREAM_ENDED]


def test_a_real_disappearance_still_ends_the_track() -> None:
    """Sampling must not hide an exit: absent for longer than max_lost_seconds ends it."""
    selected = [frame(index) for index in range(0, 64, 3)]
    records = run_pipeline(selected, present=lambda index: index < 24)
    ends = [r.summary for r in records if r.summary is not None]
    assert ends[0].end_reason is TrackEndReason.ABSENT
    assert ends[0].last_seen_ms < 24 * 62.5


def test_a_source_discontinuity_ends_old_tracks_instead_of_joining_them() -> None:
    selected = [frame(index) for index in range(0, 30, 3)]
    selected.append(frame(31, discontinuity=True))
    selected += [frame(index) for index in range(34, 50, 3)]
    records = run_pipeline(selected)
    summaries = [r.summary for r in records if r.summary is not None]
    assert summaries[0].track_id == 1
    assert summaries[0].end_reason is TrackEndReason.DISCONTINUITY
    later = {r.observation.track_id for r in records if r.observation is not None} - {1}
    assert later == {2}, "a new track id after the gap; ids are never reused within a run"


def test_a_discontinuity_on_the_very_first_frame_is_harmless() -> None:
    records = run_pipeline([frame(0, discontinuity=True), frame(3), frame(6)])
    assert not [r for r in records if r.summary and r.summary.end_reason.value == "DISCONTINUITY"]


# ================================= runtime: 16 fps source, ~100 ms detector, paced inference
def test_sixteen_fps_with_a_100ms_detector_stays_bounded_current_and_paced() -> None:
    """Items 1-4 and 8 of the stage: no queue growth, capture keeps its rate while inference is
    slower, the newest frame is chosen, the rate stays in band, and the frames not processed
    are overwhelmingly intentional skips rather than drops."""
    detector = LatencyDetector(lambda index: 0.09 + (index % 3) * 0.01)  # 90-110 ms
    source = sixteen_fps_source(48)
    runtime = LiveDemoRuntime(
        source, detector, tracking_config=CONFIG, inference_rate=InferenceRateConfig()
    )
    depths: list[int] = []
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            depths.append(runtime._scheduler.depth)
            time.sleep(0.005)

    watcher = threading.Thread(target=watch, name="depth-watcher")
    watcher.start()
    try:
        runtime.run()
    finally:
        done.set()
        watcher.join(timeout=2)
    metrics = runtime.metrics()

    assert max(depths) <= 1, "a single slot: nothing can queue behind inference"
    assert metrics["inference_queue_depth_max"] == 1
    assert metrics["camera_frames_captured_total"] == 48
    assert 12.0 <= metrics["camera_capture_fps"] <= 20.0, metrics["camera_capture_fps"]
    assert 4.0 <= metrics["effective_inference_fps"] <= 8.0, metrics["effective_inference_fps"]
    assert metrics["inference_frames_skipped_scheduler_total"] >= 20
    assert metrics["inference_frames_dropped_backpressure_total"] <= 2
    accounted = (
        metrics["inference_frames_selected_total"]
        + metrics["inference_frames_skipped_scheduler_total"]
        + metrics["inference_frames_dropped_backpressure_total"]
        + metrics["inference_frames_discarded_on_stop_total"]
    )
    assert accounted == metrics["camera_frames_captured_total"]
    assert metrics["inference_frames_processed_total"] == len(detector.seen)

    indices = [index for index, _ in detector.seen]
    assert indices == sorted(indices) and len(set(indices)) == len(indices)
    assert indices[-1] >= 44, "the final detection was on a current frame, not a backlog"
    assert max(b - a for a, b in pairwise(indices)) <= 6
    # Every frame taken for inference had waited in the slot at most ~one source interval.
    assert metrics["inference_handoff_age_ms"]["p95"] <= 1000.0 / SOURCE_FPS + 40.0
    # Original timestamps reach the detector untouched.
    assert all(stamp == index / SOURCE_FPS * 1000.0 for index, stamp in detector.seen)


def test_one_continuously_present_person_is_one_track_and_one_stable_occupancy() -> None:
    detector = LatencyDetector(0.1)
    runtime = LiveDemoRuntime(
        sixteen_fps_source(40),
        detector,
        tracking_config=CONFIG,
        inference_rate=InferenceRateConfig(),
    )
    runtime.run()
    metrics = runtime.metrics()
    assert metrics["tracks_created_total"] == 1
    assert len(events(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW)) == 1
    occupancy = [e["occupancy"] for e in events(runtime, DemoEventKind.OCCUPANCY_CHANGED)]
    # Up once when confirmed, down once when the stream ends. No flicker in between.
    assert occupancy == [1, 0], occupancy
    assert metrics["inference_frames_skipped_scheduler_total"] > 0


def test_a_single_missed_detection_does_not_drop_occupancy() -> None:
    detector = LatencyDetector(0.0, present=lambda index: index != 12)
    source = FakeLiveSource(
        frame_count=24, width=WIDTH, height=HEIGHT, fps=SOURCE_FPS, interval_seconds=0.01
    )
    runtime = LiveDemoRuntime(source, detector, tracking_config=CONFIG)
    runtime.run()
    occupancy = [e["occupancy"] for e in events(runtime, DemoEventKind.OCCUPANCY_CHANGED)]
    assert occupancy == [1, 0], occupancy
    assert len(events(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW)) == 1


def test_an_actual_exit_is_still_reported_while_the_session_continues() -> None:
    detector = LatencyDetector(0.1, present=lambda index: index < 16)
    runtime = LiveDemoRuntime(
        sixteen_fps_source(48),
        detector,
        tracking_config=CONFIG,
        inference_rate=InferenceRateConfig(),
    )
    runtime.run()
    kinds = [e["kind"] for e in reversed(runtime.timeline.recent(200))]
    gone = kinds.index("PERSON_NO_LONGER_VISIBLE")
    assert gone < kinds.index("TRACKING_STOPPED")
    assert "OCCUPANCY_CHANGED" in kinds[: gone + 2]
    assert runtime.timeline.occupancy == 0
    assert runtime.timeline.peak_occupancy == 1


def test_a_reconnect_is_not_joined_to_the_state_before_it() -> None:
    detector = LatencyDetector(0.0)
    source = DiscontinuousSource(
        discontinuity_at=15,
        frame_count=30,
        width=WIDTH,
        height=HEIGHT,
        fps=SOURCE_FPS,
        interval_seconds=0.01,
    )
    runtime = LiveDemoRuntime(source, detector, tracking_config=CONFIG)
    runtime.run()
    appeared = [e["track_id"] for e in events(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW)]
    gone = [e["track_id"] for e in events(runtime, DemoEventKind.PERSON_NO_LONGER_VISIBLE)]
    assert appeared == [1, 2], "the track after the reconnect is a new track"
    assert gone[0] == 1
    assert runtime.timeline.peak_occupancy == 1, "old and new are never counted together"
    metrics = runtime.metrics()
    assert metrics["tracking_discontinuities_total"] == 1
    assert metrics["source_discontinuities_total"] == 1


def test_preview_cadence_is_independent_of_the_detector_cadence() -> None:
    def run(preview_fps: float) -> tuple[LiveDemoRuntime, PreviewRenderer, LatencyDetector]:
        detector = LatencyDetector(0.02)
        preview = PreviewRenderer(PreviewConfig(target_fps=preview_fps))
        runtime = LiveDemoRuntime(
            sixteen_fps_source(24),
            detector,
            tracking_config=CONFIG,
            preview=preview,
            inference_rate=InferenceRateConfig(target_fps=8.0),
        )
        runtime.run()
        return runtime, preview, detector

    slow_runtime, slow_preview, slow_detector = run(1.0)
    fast_runtime, fast_preview, fast_detector = run(30.0)
    # The preview throttle skips frames on its own; the detector's schedule does not notice.
    assert slow_preview.previews_skipped_total > 0
    assert abs(len(slow_detector.seen) - len(fast_detector.seen)) <= 2
    fast = fast_runtime.metrics()
    assert fast["preview_frames_encoded_total"] <= fast["inference_frames_processed_total"]
    assert fast["preview_fps"] <= fast["effective_inference_fps"] + 0.5


# ======================================================================= lifecycle safety
def test_stop_while_inference_is_running_completes_boundedly() -> None:
    detector = LatencyDetector(0.4)
    runtime = LiveDemoRuntime(
        sixteen_fps_source(10_000),
        detector,
        tracking_config=CONFIG,
        inference_rate=InferenceRateConfig(),
    )
    runtime.start()
    assert detector.entered.wait(5.0)
    started = time.monotonic()
    runtime.stop()
    assert time.monotonic() - started < 2.0
    assert not runtime.running
    assert wait_for_no_live_threads() == []


def test_a_detector_failure_stops_and_cleans_up() -> None:
    source = sixteen_fps_source(10_000)
    runtime = LiveDemoRuntime(
        source,
        LatencyDetector(0.05, fail_on_call=3),
        tracking_config=CONFIG,
        inference_rate=InferenceRateConfig(),
    )
    runtime.run()
    assert runtime.failure == "pipeline_error"
    assert source.closed
    assert wait_for_no_live_threads() == []


def test_a_source_failure_stops_and_cleans_up() -> None:
    source = sixteen_fps_source(100, fail_after=6)
    runtime = LiveDemoRuntime(
        source, LatencyDetector(0.05), tracking_config=CONFIG, inference_rate=InferenceRateConfig()
    )
    runtime.run()
    assert runtime.failure == "camera_unavailable"
    assert wait_for_no_live_threads() == []


def test_repeated_start_stop_cycles_leak_no_threads() -> None:
    baseline = threading.active_count()
    for _ in range(8):
        runtime = LiveDemoRuntime(
            sixteen_fps_source(10_000),
            LatencyDetector(0.03),
            tracking_config=CONFIG,
            inference_rate=InferenceRateConfig(),
        )
        runtime.start()
        time.sleep(0.12)
        runtime.stop()
    assert wait_for_no_live_threads() == []
    assert threading.active_count() <= baseline


# ========================================================================= observability
V1_03A_KEYS = (
    "camera_frames_captured_total",
    "inference_frames_selected_total",
    "inference_frames_processed_total",
    "inference_frames_skipped_scheduler_total",
    "inference_frames_dropped_backpressure_total",
    "inference_frames_discarded_on_stop_total",
    "effective_inference_fps",
    "inference_scheduled_fps",
    "inference_scheduler_state",
    "preview_frames_encoded_total",
    "preview_frames_skipped_total",
    "preview_fps",
    "camera_capture_fps",
    "detector_latency_ms",
    "tracker_latency_ms",
    "pipeline_latency_ms",
    "camera_reconnect_count",
    "occupancy",
    "active_tracks",
    "source_frames_dropped_total",
)


def test_the_dashboard_serialises_every_new_metric() -> None:
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    runtime = LiveDemoRuntime(
        sixteen_fps_source(16),
        LatencyDetector(0.02),
        tracking_config=CONFIG,
        preview=preview,
        inference_rate=InferenceRateConfig(),
    )
    runtime.run()
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5) as response:
            payload = json.loads(response.read())
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
    metrics = payload["metrics"]
    for key in V1_03A_KEYS:
        assert key in metrics, key
    # A synthetic source cannot measure transport loss, and says so rather than claiming zero.
    assert metrics["source_frames_dropped_total"] is None
    for label in (
        "Capture FPS",
        "Inference FPS",
        "Preview FPS",
        "Inference scheduler skips",
        "Backpressure drops",
        "Transport/media drops",
        "Reconnects",
        "People currently visible",
        "local evaluation &mdash; no identification",
    ):
        assert label in page, label
    for forbidden in ("identity", "face", "staff", "embedding", "child"):
        assert not any(forbidden in key for key in metrics), forbidden


# ================================================================ CLI: camera and synthetic
def demo_arguments(*extra: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    live_cli.add_demo_arguments(parser)
    return parser.parse_args(list(extra))


def test_the_cli_paces_inference_by_default_for_every_source() -> None:
    for source in ("camera", "synthetic", "ring"):
        arguments = demo_arguments("--source", source)
        config = InferenceRateConfig(
            target_fps=arguments.inference_fps,
            min_fps=arguments.inference_min_fps,
            max_fps=arguments.inference_max_fps,
        )
        assert (config.min_fps, config.target_fps, config.max_fps) == (3.0, 6.0, 8.0)


def test_an_invalid_rate_is_refused_before_anything_is_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(_arguments: Any) -> Any:
        raise AssertionError("nothing may be opened or started for a refused setting")

    monkeypatch.setattr(live_cli, "_source", must_not_run)
    monkeypatch.setattr(live_cli, "_detector", must_not_run)
    arguments = demo_arguments("--source", "camera", "--inference-fps", "20")
    assert live_cli.run_demo_cli(arguments) == 2


def test_the_synthetic_demo_runs_paced_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = demo_arguments(
        "--source",
        "synthetic",
        "--detector",
        "none",
        "--headless",
        "--max-frames",
        "12",
        "--fps",
        "16",
        "--width",
        "320",
        "--height",
        "240",
    )
    assert live_cli.run_demo_cli(arguments) == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") :])
    assert report["failure"] is None
    assert report["metrics"]["inference_pacing"] == "adaptive"
    assert report["metrics"]["inference_frames_processed_total"] >= 1
    assert wait_for_no_live_threads() == []
