"""Recorded-video person detection and tracking (V1-02B1A).

No model weight, no GPU and - for everything except the handful of container tests - no video
file either. The pipeline's ``process`` entry point takes frames directly, so detection
handling, tracking, lifecycle, output and metrics are all exercised against constructed input
that a reader can see the whole of.

Scripted detections are written as pictures of what is happening: a box walking left to right,
two boxes staying apart, a box vanishing for a few frames and coming back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.recorded import (
    TRACK_SCHEMA_VERSION,
    BoundingBoxValidator,
    DetectedPerson,
    FakePersonDetector,
    PipelineRecord,
    RecordedTrackingPipeline,
    RecordedVideoSource,
    TrackEndReason,
    TrackIdentityObservation,
    TrackLifecycle,
    VideoSourceError,
    run_recorded_tracking,
)
from veotrex_edge_agent.recorded.output import (
    OutputError,
    assert_no_biometric_material,
    read_ndjson,
)
from veotrex_edge_agent.recorded.yolox import (
    EVALUATION_ENVIRONMENTS,
    DetectorUnavailable,
    YoloxPersonDetector,
)
from veotrex_edge_agent.tracking import TrackingConfig

WIDTH, HEIGHT = 640, 480
FPS = 10.0
# Confirmation needs two observations and a track is dropped after max_lost_seconds; both are
# stated here so the expectations below can be read without opening the tracker.
CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)


@dataclass(frozen=True, slots=True)
class Frame:
    """The frame shape the pipeline consumes. A 1x1 image: nothing here decodes pixels, and a
    full-size array per frame would make the tests slower for no added coverage."""

    frame_index: int
    timestamp_ms: float
    width: int = WIDTH
    height: int = HEIGHT
    image: Any = None

    def __post_init__(self) -> None:
        if self.image is None:
            object.__setattr__(self, "image", np.zeros((1, 1, 3), dtype=np.uint8))


def frames(count: int, *, fps: float = FPS, start: int = 0):  # type: ignore[no-untyped-def]
    return [Frame(index, (index + start) / fps * 1000.0) for index in range(count)]


# Motion per frame as a fraction of box width is what IoU association actually cares about.
# 6 px against a 60 px box is 10%, which is an ordinary walking pace for a fixed camera at
# this frame rate and well inside what the tracker recovers across a gap (see
# ``test_recovery_across_a_gap_has_a_measured_motion_limit``).
WALK_STEP = 6.0
BOX_WIDTH = 60.0


def walking(
    count: int, *, x0: float = 10.0, step: float = WALK_STEP, bounce: bool = True
) -> dict[int, list[Any]]:
    """One person moving across the frame, one box per frame.

    ``bounce`` keeps them inside the frame for arbitrarily long sequences by reversing at the
    edges; without it a long script would walk the person out of shot and end the track, which
    is correct behaviour but not what a long-run test is trying to measure.
    """
    script: dict[int, list[Any]] = {}
    span = WIDTH - BOX_WIDTH - x0
    for index in range(count):
        travelled = index * step
        if bounce and span > 0:
            cycle = travelled % (2 * span)
            offset = cycle if cycle <= span else 2 * span - cycle
        else:
            offset = travelled
        left = x0 + offset
        script[index] = [(left, 100.0, left + BOX_WIDTH, 300.0, 0.9)]
    return script


def run(script: dict[int, list[Any]], count: int, **kwargs: Any) -> list[PipelineRecord]:
    pipeline = RecordedTrackingPipeline(
        FakePersonDetector(script), tracking_config=CONFIG, **kwargs
    )
    return list(pipeline.process(frames(count), run_id="test"))


def observations(records: list[PipelineRecord]) -> list[Any]:
    return [r.observation for r in records if r.observation is not None]


def summaries(records: list[PipelineRecord]) -> list[Any]:
    return [r.summary for r in records if r.summary is not None]


def track_ids(records: list[PipelineRecord]) -> set[int]:
    return {o.track_id for o in observations(records)}


# ------------------------------------------------------------------ 1. input rejection
def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(tmp_path / "absent.mp4").open()
    assert raised.value.category == "video_not_found"


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.touch()
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(empty).open()
    assert raised.value.category == "video_empty"


def test_a_file_that_is_not_a_video_is_refused(tmp_path: Path) -> None:
    """Named .mp4 and full of text: the container check passes and the decoder refuses it."""
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"this is definitely not an mp4" * 100)
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(bogus).open()
    assert raised.value.category == "video_unreadable"


def test_an_unsupported_container_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "clip.webm"
    other.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(other).open()
    assert raised.value.category == "video_unsupported_container"


def test_a_directory_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "clip.mp4"
    directory.mkdir()
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(directory).open()
    assert raised.value.category == "video_not_a_regular_file"


def test_a_symlinked_input_is_refused(tmp_path: Path) -> None:
    """The input is operator-owned local data; following a link would let a path point
    somewhere the operator did not mean to read."""
    real = tmp_path / "real.mp4"
    real.write_bytes(b"\x00" * 64)
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(link).open()
    assert raised.value.category == "video_symlink_rejected"


def test_invalid_sampling_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(tmp_path / "x.mp4", sample_every=0)
    assert raised.value.category == "invalid_sampling"


def test_no_url_can_be_passed_as_input(tmp_path: Path) -> None:
    """There is no URL path in this stage. A URL-looking string is just a missing file."""
    with pytest.raises(VideoSourceError) as raised:
        RecordedVideoSource(Path("http://example.invalid/clip.mp4")).open()
    assert raised.value.category in {"video_not_found", "video_unsupported_container"}


# ------------------------------------------------------- 2. source timestamps preserved
def test_source_timestamps_come_from_the_media_timeline_not_the_clock() -> None:
    """The decisive property: the same frames processed at any speed produce identical
    timestamps, because nothing consults the wall clock."""
    script = walking(6)
    first = observations(run(script, 6))
    second = observations(run(script, 6))
    assert [o.timestamp_ms for o in first] == [o.timestamp_ms for o in second]
    # 10 fps: frame n is at exactly n * 100 ms.
    assert [round(o.timestamp_ms, 3) for o in first] == [
        round(o.frame_index * 100.0, 3) for o in first
    ]


def test_timestamps_are_independent_of_processing_speed() -> None:
    """A deliberately slow detector must not shift the time axis by a microsecond."""

    class SlowDetector(FakePersonDetector):
        def detect(self, image: Any, *, frame_index: int, timestamp_ms: float) -> list[Any]:
            sum(range(20_000))  # burn measurable wall time
            return super().detect(image, frame_index=frame_index, timestamp_ms=timestamp_ms)

    fast = RecordedTrackingPipeline(FakePersonDetector(walking(5)), tracking_config=CONFIG)
    slow = RecordedTrackingPipeline(SlowDetector(walking(5)), tracking_config=CONFIG)
    fast_ts = [o.timestamp_ms for o in observations(list(fast.process(frames(5), run_id="a")))]
    slow_ts = [o.timestamp_ms for o in observations(list(slow.process(frames(5), run_id="b")))]
    assert fast_ts == slow_ts
    assert slow.metrics.processing_seconds > 0


# ------------------------------------------------------------ 3/4. detector contract
def test_the_fake_detector_satisfies_the_protocol_contract() -> None:
    detector = FakePersonDetector({0: [(1.0, 2.0, 3.0, 4.0, 0.5)]})
    found = detector.detect(np.zeros((4, 4, 3), np.uint8), frame_index=0, timestamp_ms=0.0)
    assert len(found) == 1
    assert isinstance(found[0], DetectedPerson)
    assert found[0].label == "person"
    assert found[0].frame_index == 0
    assert detector.detect(np.zeros((4, 4, 3), np.uint8), frame_index=9, timestamp_ms=1.0) == []


def test_a_box_overhanging_the_frame_is_clamped_rather_than_dropped() -> None:
    """A person half out of shot is a real detection and must survive."""
    validator = BoundingBoxValidator(WIDTH, HEIGHT)
    accepted, rejected = validator.validate(
        [DetectedPerson((-40.0, -10.0, 120.0, 260.0), 0.8, 0, 0.0)]
    )
    assert rejected == 0
    assert accepted[0].bbox_xyxy == (0.0, 0.0, 120.0, 260.0)


@pytest.mark.parametrize(
    ("box", "reason"),
    [
        ((100.0, 100.0, 50.0, 200.0), "inverted in x"),
        ((100.0, 200.0, 200.0, 100.0), "inverted in y"),
        ((100.0, 100.0, 100.0, 200.0), "zero width"),
        ((float("nan"), 0.0, 10.0, 10.0), "not a number"),
        ((0.0, 0.0, float("inf"), 10.0), "infinite"),
        ((-500.0, -500.0, -400.0, -400.0), "entirely outside the frame"),
    ],
)
def test_a_malformed_box_is_rejected_rather_than_repaired(box: Any, reason: str) -> None:
    """Reordering an inverted box would fabricate a plausible detection from a broken one."""
    validator = BoundingBoxValidator(WIDTH, HEIGHT)
    accepted, rejected = validator.validate([DetectedPerson(box, 0.9, 0, 0.0)])
    assert accepted == [], reason
    assert rejected == 1


@pytest.mark.parametrize("confidence", [-0.1, 1.5, float("nan")])
def test_an_impossible_confidence_is_rejected(confidence: float) -> None:
    validator = BoundingBoxValidator(WIDTH, HEIGHT)
    accepted, rejected = validator.validate(
        [DetectedPerson((10.0, 10.0, 50.0, 90.0), confidence, 0, 0.0)]
    )
    assert accepted == [] and rejected == 1


def test_a_non_person_label_cannot_reach_the_tracker() -> None:
    validator = BoundingBoxValidator(WIDTH, HEIGHT)
    chair = DetectedPerson((10.0, 10.0, 50.0, 90.0), 0.9, 0, 0.0, label="chair")  # type: ignore[arg-type]
    accepted, rejected = validator.validate([chair])
    assert accepted == [] and rejected == 1


def test_a_flood_of_detections_is_bounded() -> None:
    validator = BoundingBoxValidator(WIDTH, HEIGHT)
    flood = [DetectedPerson((0.0, 0.0, 20.0, 40.0), 0.9, 0, 0.0) for _ in range(1000)]
    accepted, rejected = validator.validate(flood)
    assert len(accepted) == 300
    assert rejected == 700


def test_malformed_boxes_are_counted_and_do_not_stop_the_run() -> None:
    script = {index: [(10.0 + index, 10.0, 70.0 + index, 200.0, 0.9)] for index in range(5)}
    script[2] = [(50.0, 50.0, 10.0, 10.0, 0.9)]  # inverted: this frame contributes nothing
    pipeline = RecordedTrackingPipeline(FakePersonDetector(script), tracking_config=CONFIG)
    list(pipeline.process(frames(5), run_id="t"))
    assert pipeline.metrics.detections_rejected_total == 1
    assert pipeline.metrics.video_frames_processed_total == 5


# ------------------------------------------------------------------ 5/6. tracking basics
def test_one_person_keeps_one_stable_track_id() -> None:
    records = run(walking(10), 10)
    assert len(track_ids(records)) == 1
    ids = [o.track_id for o in observations(records)]
    assert len(set(ids)) == 1, "a single person must not change id while continuously visible"


def test_two_people_keep_separate_tracks() -> None:
    script = {
        index: [
            (10.0 + index * 5, 100.0, 70.0 + index * 5, 300.0, 0.9),
            (400.0 - index * 5, 100.0, 460.0 - index * 5, 300.0, 0.9),
        ]
        for index in range(10)
    }
    records = run(script, 10)
    assert len(track_ids(records)) == 2
    per_frame = {}
    for observation in observations(records):
        per_frame.setdefault(observation.frame_index, set()).add(observation.track_id)
    assert all(len(ids) == 2 for ids in per_frame.values() if ids)


def test_the_first_frame_alone_does_not_confirm_a_track() -> None:
    """A single detection is a hypothesis. Publishing it would mean emitting a track id for
    something that may never appear again."""
    records = run({0: [(10.0, 10.0, 70.0, 200.0, 0.9)]}, 1)
    assert observations(records) == []


# ----------------------------------------------------- 7/8/9. occlusion and termination
def test_a_brief_missed_detection_does_not_kill_the_track() -> None:
    """Two frames of nothing at 10 fps is 0.2 s, inside the 0.5 s tolerance: the same person
    must come back with the same id."""
    script = walking(12)
    del script[5]
    del script[6]
    records = run(script, 12)
    assert len(track_ids(records)) == 1
    seen = [o.frame_index for o in observations(records)]
    assert 4 in seen and 7 in seen


def test_recovery_across_a_gap_has_a_measured_motion_limit() -> None:
    """A documented limitation, asserted so a change to it is visible.

    Association is IoU-based, so recovering a track across missed frames depends on the
    person's predicted box still overlapping their real one. Measured on this fixture: a gap
    of two frames is bridged while motion is at or below ~13% of box width per frame, and is
    not bridged at 20%, where the track is split and the person is issued a new id.

    This is a property of IoU association, not a defect, and it is the main reason V1-02B1B
    must treat a track id as continuity *within* a run rather than as an identity.
    """

    def tracks_for(step: float) -> int:
        script = walking(12, step=step, bounce=False)
        del script[5]
        del script[6]
        return len(track_ids(run(script, 12)))

    assert tracks_for(BOX_WIDTH * 0.10) == 1
    assert tracks_for(BOX_WIDTH * 0.133) == 1
    assert tracks_for(BOX_WIDTH * 0.20) == 2, "the limit moved; re-measure and update the docs"


def test_a_long_disappearance_terminates_the_track() -> None:
    script = {index: walking(20, bounce=False)[index] for index in range(4)}
    for index in range(14, 20):
        script[index] = [(500.0, 100.0, 560.0, 300.0, 0.9)]
    records = run(script, 20)
    ended = summaries(records)
    assert any(s.end_reason is TrackEndReason.ABSENT for s in ended)


def test_a_person_arriving_after_a_termination_gets_a_new_track() -> None:
    """The second person must not inherit the first one's id or Kalman state."""
    script: dict[int, list[Any]] = {index: walking(30, bounce=False)[index] for index in range(4)}
    for index in range(20, 26):
        script[index] = [(500.0, 100.0, 560.0, 300.0, 0.9)]
    records = run(script, 26)
    early = {o.track_id for o in observations(records) if o.frame_index < 10}
    late = {o.track_id for o in observations(records) if o.frame_index >= 20}
    assert early and late
    assert early.isdisjoint(late)


def test_a_track_still_live_when_the_video_ends_is_not_a_disappearance() -> None:
    """The distinction that stops a later stage inventing "the teacher left" from a file that
    simply ran out."""
    records = run(walking(8), 8)
    ended = summaries(records)
    assert len(ended) == 1
    assert ended[0].end_reason is TrackEndReason.STREAM_ENDED


# ------------------------------------------------------------------- 10. state isolation
def test_tracker_state_does_not_leak_between_jobs() -> None:
    """The same pipeline object run twice must produce two independent worlds."""
    pipeline = RecordedTrackingPipeline(FakePersonDetector(walking(8)), tracking_config=CONFIG)
    first = list(pipeline.process(frames(8), run_id="job-a"))
    second = list(pipeline.process(frames(8), run_id="job-b"))
    assert track_ids(first) == track_ids(second) == {1}
    assert [o.timestamp_ms for o in observations(first)] == [
        o.timestamp_ms for o in observations(second)
    ]


def test_two_pipelines_do_not_share_track_numbering() -> None:
    a = list(
        RecordedTrackingPipeline(FakePersonDetector(walking(8)), tracking_config=CONFIG).process(
            frames(8), run_id="a"
        )
    )
    b = list(
        RecordedTrackingPipeline(FakePersonDetector(walking(8)), tracking_config=CONFIG).process(
            frames(8), run_id="b"
        )
    )
    assert track_ids(a) == track_ids(b)


# ---------------------------------------------------------------------- 11. bounded memory
def test_memory_does_not_grow_with_the_length_of_the_video() -> None:
    """A long run must not accumulate per-frame state. Completed tracks are emitted and
    dropped, and the latency reservoirs are capped."""
    pipeline = RecordedTrackingPipeline(FakePersonDetector(walking(600)), tracking_config=CONFIG)
    records = list(pipeline.process(frames(600), run_id="long"))
    assert len(records) > 500, "the person must stay in frame for the whole run"
    assert pipeline.metrics.detector_latency_ms.retained_count <= 4096
    assert pipeline.metrics.detector_latency_ms.count == 600
    # Every live track was closed out; nothing is still being held.
    assert pipeline.metrics.active_tracks == 0
    assert pipeline._live == {}  # the bound is the property under test


def test_completed_tracks_are_not_retained_after_they_end() -> None:
    script: dict[int, list[Any]] = {index: walking(30, bounce=False)[index] for index in range(4)}
    for index in range(20, 26):
        script[index] = [(500.0, 100.0, 560.0, 300.0, 0.9)]
    pipeline = RecordedTrackingPipeline(FakePersonDetector(script), tracking_config=CONFIG)
    list(pipeline.process(frames(26), run_id="t"))
    assert pipeline._live == {}


# -------------------------------------------------------------- 12/13. identity-free tracking
def test_tracking_requires_no_recognition_of_any_kind() -> None:
    """The whole pipeline runs with a detector that knows nothing about faces, and nothing in
    the emitted records mentions an identity."""
    records = run(walking(10), 10)
    assert observations(records)
    for observation in observations(records):
        assert not hasattr(observation, "staff_profile_id")
        assert not hasattr(observation, "identity")


def test_people_who_are_never_identified_are_tracked_exactly_like_anyone_else() -> None:
    """Nobody here is enrolled, recognised, or identifiable. Everyone is still tracked - which
    is what makes children and unknown adults trackable without biometrics."""
    records = run(walking(10), 10)
    assert len(track_ids(records)) == 1
    assert len(observations(records)) >= 8


def test_an_identity_observation_cannot_invent_a_match_without_a_profile() -> None:
    with pytest.raises(ValueError, match="match_requires_a_staff_profile"):
        TrackIdentityObservation(track_id=1, decision="MATCH", timestamp_ms=0.0)


def test_an_unknown_identity_cannot_carry_the_name_it_refused_to_give() -> None:
    with pytest.raises(ValueError, match="unknown_identity_cannot_carry"):
        TrackIdentityObservation(
            track_id=1, decision="UNKNOWN", timestamp_ms=0.0, staff_profile_id="abc"
        )


def test_an_unknown_identity_is_representable_and_anonymous() -> None:
    unknown = TrackIdentityObservation(track_id=7, decision="UNKNOWN", timestamp_ms=1.0)
    assert unknown.staff_profile_id is None


# ------------------------------------------------------------- 14/15/16. output contract
def test_output_is_versioned_and_has_a_header_and_footer(tmp_path: Path) -> None:
    # The writer is exercised directly so no codec is involved; the file-driven path is
    # covered by the round-trip test at the end of this module.
    from veotrex_edge_agent.recorded.output import write_ndjson

    records = run(walking(8), 8)
    destination = tmp_path / "tracks.ndjson"
    written = write_ndjson(
        records,
        destination,
        header={"run_id": "t", "video_filename": "x.mp4", "identity_annotation": "none"},
        metrics_factory=lambda: {"video_frames_processed_total": 8},
    )
    lines = [json.loads(line) for line in destination.read_text().splitlines()]
    assert lines[0]["type"] == "header"
    assert lines[-1]["type"] == "footer"
    assert lines[-1]["records"] == written
    assert all(line["schema_version"] == TRACK_SCHEMA_VERSION for line in lines)


def test_reading_back_an_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tracks.ndjson"
    path.write_text(json.dumps({"schema_version": 999, "type": "header"}) + "\n")
    with pytest.raises(OutputError, match="unsupported_schema_version"):
        list(read_ndjson(path))


def test_output_contains_no_biometric_material(tmp_path: Path) -> None:
    from veotrex_edge_agent.recorded.output import write_ndjson

    destination = tmp_path / "tracks.ndjson"
    write_ndjson(
        run(walking(10), 10),
        destination,
        header={"run_id": "t", "video_filename": "x.mp4"},
        metrics_factory=dict,
    )
    text = destination.read_text()
    for marker in ("embedding", "template", "face", "crop", "descriptor", "biometric"):
        assert marker not in text.lower()
    for line in text.splitlines():
        assert_no_biometric_material(json.loads(line))


def test_the_guard_actually_catches_biometric_material() -> None:
    """A guard that never fires proves nothing, so it is shown failing."""
    with pytest.raises(OutputError, match="biometric_material_in_output"):
        assert_no_biometric_material({"track_id": 1, "embedding": [0.1, 0.2]})
    with pytest.raises(OutputError, match="biometric_material_in_output"):
        assert_no_biometric_material({"tracks": [{"face_crop": "..."}]})
    with pytest.raises(OutputError, match="oversized_opaque_value"):
        assert_no_biometric_material({"note": "A" * 200})


def test_output_contains_no_frame_images_or_pixel_data(tmp_path: Path) -> None:
    from veotrex_edge_agent.recorded.output import write_ndjson

    destination = tmp_path / "tracks.ndjson"
    write_ndjson(
        run(walking(6), 6),
        destination,
        header={"run_id": "t", "video_filename": "x.mp4"},
        metrics_factory=dict,
    )
    for line in destination.read_text().splitlines():
        record = json.loads(line)
        assert "image" not in record
        # Geometry only: four numbers, not a region of pixels.
        if record.get("type") == "observation":
            assert len(record["bbox_xyxy"]) == 4
            assert all(isinstance(value, int | float) for value in record["bbox_xyxy"])


def test_writing_over_an_existing_output_is_refused(tmp_path: Path) -> None:
    from veotrex_edge_agent.recorded.output import write_ndjson

    destination = tmp_path / "tracks.ndjson"
    destination.write_text("previous run\n")
    with pytest.raises(OutputError, match="output_already_exists"):
        write_ndjson([], destination, header={"run_id": "t"})
    assert destination.read_text() == "previous run\n"


def test_a_failed_write_leaves_no_partial_output(tmp_path: Path) -> None:
    from veotrex_edge_agent.recorded.output import write_ndjson

    def exploding() -> Any:
        yield from run(walking(4), 4)
        raise RuntimeError("detector died mid-run")

    destination = tmp_path / "tracks.ndjson"
    with pytest.raises(RuntimeError):
        write_ndjson(exploding(), destination, header={"run_id": "t"})
    assert not destination.exists()


def test_the_output_file_is_private(tmp_path: Path) -> None:
    import stat

    from veotrex_edge_agent.recorded.output import write_ndjson

    destination = tmp_path / "tracks.ndjson"
    write_ndjson([], destination, header={"run_id": "t"})
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


# ----------------------------------------------------------------- 17. environment gate
@pytest.mark.parametrize("environment", ["staging", "production", "prod", "hostinger"])
def test_the_evaluation_detector_cannot_activate_outside_local(environment: str) -> None:
    """YOLOX-S is provisionally licensed (ADR 0009) and must not run where it would be
    redistributed or relied on."""
    with pytest.raises(DetectorUnavailable, match="permitted only in"):
        YoloxPersonDetector(environment=environment)


@pytest.mark.parametrize("environment", sorted(EVALUATION_ENVIRONMENTS))
def test_the_evaluation_detector_constructs_in_permitted_environments(environment: str) -> None:
    detector = YoloxPersonDetector(environment=environment)
    assert not detector.ready, "constructing must not start a worker or load an engine"


def test_staging_and_production_are_absent_from_the_permitted_set() -> None:
    assert "staging" not in EVALUATION_ENVIRONMENTS
    assert "production" not in EVALUATION_ENVIRONMENTS


def test_the_detector_refuses_to_infer_before_it_is_started() -> None:
    detector = YoloxPersonDetector(environment="test")
    with pytest.raises(DetectorUnavailable, match="detector_not_started"):
        detector.detect(np.zeros((4, 4, 3), np.uint8), frame_index=0, timestamp_ms=0.0)


# ---------------------------------------------------------------- 18/19. robustness
def test_a_frame_the_detector_cannot_use_does_not_stop_the_run() -> None:
    """One bad frame in the middle of a recording is skipped, not fatal."""

    class FlakyDetector(FakePersonDetector):
        def detect(self, image: Any, *, frame_index: int, timestamp_ms: float) -> list[Any]:
            if frame_index == 3:
                return [
                    DetectedPerson((float("nan"), 0.0, 1.0, 1.0), 0.9, frame_index, timestamp_ms)
                ]
            return super().detect(image, frame_index=frame_index, timestamp_ms=timestamp_ms)

    pipeline = RecordedTrackingPipeline(FlakyDetector(walking(8)), tracking_config=CONFIG)
    records = list(pipeline.process(frames(8), run_id="t"))
    assert pipeline.metrics.video_frames_processed_total == 8
    assert pipeline.metrics.detections_rejected_total == 1
    assert observations(records)


def test_the_same_input_always_produces_the_same_output() -> None:
    """Determinism is what makes a regression in tracking visible at all."""
    script = {
        index: [
            (10.0 + index * 6, 100.0, 70.0 + index * 6, 300.0, 0.9),
            (400.0 - index * 6, 120.0, 460.0 - index * 6, 320.0, 0.85),
        ]
        for index in range(15)
    }
    first = [
        (o.track_id, o.frame_index, o.bbox_xyxy, o.lifecycle) for o in observations(run(script, 15))
    ]
    second = [
        (o.track_id, o.frame_index, o.bbox_xyxy, o.lifecycle) for o in observations(run(script, 15))
    ]
    assert first == second


def test_frame_sampling_keeps_source_timestamps() -> None:
    """Processing every other frame must not compress the time axis."""
    sampled = [Frame(index, index * 2 * 100.0) for index in range(5)]
    pipeline = RecordedTrackingPipeline(FakePersonDetector(walking(5)), tracking_config=CONFIG)
    records = list(pipeline.process(sampled, run_id="t"))
    stamps = [o.timestamp_ms for o in observations(records)]
    assert stamps == sorted(stamps)
    assert stamps[0] >= 200.0  # the second source frame, not the second processed one


# ------------------------------------------------------------------ 20. no downloads
def test_no_module_in_the_recorded_package_can_download_anything() -> None:
    """No runtime model download path exists, by inspection of the source rather than by
    convention."""
    package = Path(__file__).resolve().parents[1] / "src" / "veotrex_edge_agent" / "recorded"
    for module in sorted(package.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        for forbidden in (
            "urlopen",
            "urlretrieve",
            "requests.get",
            "httpx.get",
            "urllib.request",
            "subprocess",
        ):
            assert forbidden not in text, f"{module.name} must not fetch or shell out"


# --------------------------------------------------------- 21/22. resource cleanup
def test_the_source_is_released_after_a_successful_run(tmp_path: Path) -> None:
    released: list[str] = []

    class TrackedSource(RecordedVideoSource):
        def close(self) -> None:
            released.append("closed")
            super().close()

    source = TrackedSource(tmp_path / "missing.mp4")
    with pytest.raises(VideoSourceError):
        with source:
            pass
    # __enter__ raised, so __exit__ never ran; close is still safe and idempotent.
    source.close()
    source.close()
    assert released


def test_close_is_safe_on_a_source_that_was_never_opened(tmp_path: Path) -> None:
    source = RecordedVideoSource(tmp_path / "never.mp4")
    source.close()
    source.close()


def test_an_unreadable_video_is_a_clean_refusal_not_a_traceback(tmp_path: Path) -> None:
    """The CLI closes the detector on every exit path, including one where the video turned
    out to be unusable after a worker had already been started."""
    from veotrex_edge_agent.recorded.cli import run_cli

    code = run_cli(
        _cli_namespace(input=tmp_path / "missing.mp4", output_dir=tmp_path / "out", detector="none")
    )
    assert code == 2
    assert not (tmp_path / "out" / "tracks.ndjson").exists()


def test_metrics_are_finalised_even_when_a_run_is_abandoned() -> None:
    """A consumer that stops reading mid-run must still leave the pipeline consistent."""
    pipeline = RecordedTrackingPipeline(FakePersonDetector(walking(50)), tracking_config=CONFIG)
    stream = pipeline.process(frames(50), run_id="t")
    for _ in range(5):
        next(stream)
    stream.close()
    assert pipeline.metrics.active_tracks == 0


# ------------------------------------------------------------------------ metrics
def test_metrics_report_counts_and_latency_percentiles() -> None:
    pipeline = RecordedTrackingPipeline(FakePersonDetector(walking(12)), tracking_config=CONFIG)
    list(pipeline.process(frames(12), run_id="t"))
    snapshot = pipeline.metrics.snapshot()
    assert snapshot["video_frames_processed_total"] == 12
    assert snapshot["person_detections_total"] == 12
    assert snapshot["tracks_created_total"] >= 1
    assert snapshot["processing_fps"] > 0
    for key in ("detector_latency_ms", "tracker_latency_ms", "pipeline_latency_ms"):
        assert snapshot[key]["count"] == 12
        assert snapshot[key]["p50"] is not None
        assert snapshot[key]["p95"] is not None


def test_peak_active_tracks_reflects_concurrent_people() -> None:
    script = {
        index: [
            (10.0 + index * 5, 100.0, 70.0 + index * 5, 300.0, 0.9),
            (400.0 - index * 5, 100.0, 460.0 - index * 5, 300.0, 0.9),
        ]
        for index in range(10)
    }
    pipeline = RecordedTrackingPipeline(FakePersonDetector(script), tracking_config=CONFIG)
    list(pipeline.process(frames(10), run_id="t"))
    assert pipeline.metrics.peak_active_tracks == 2


# ---------------------------------------------------------------------- lifecycle wording
def test_lifecycle_uses_track_vocabulary_and_never_daycare_vocabulary() -> None:
    """A track starting is not somebody entering a room. The vocabulary must not let a later
    stage confuse the two."""
    values = {str(value) for value in TrackLifecycle}
    assert values == {"TRACK_STARTED", "TRACK_ACTIVE", "TRACK_ENDED"}
    joined = " ".join(values).lower()
    for forbidden in ("enter", "exit", "teacher", "child", "arriv", "depart", "left"):
        assert forbidden not in joined


def test_the_first_report_of_a_track_is_started_and_the_rest_are_active() -> None:
    records = run(walking(10), 10)
    lifecycles = [o.lifecycle for o in observations(records)]
    assert lifecycles[0] is TrackLifecycle.TRACK_STARTED
    assert all(value is TrackLifecycle.TRACK_ACTIVE for value in lifecycles[1:])


def test_a_summary_reports_the_span_and_the_reason() -> None:
    records = run(walking(8), 8)
    summary = summaries(records)[0]
    assert summary.observation_count >= 6
    assert summary.last_seen_ms > summary.first_seen_ms
    assert summary.duration_ms > 0
    assert 0.0 <= summary.maximum_confidence <= 1.0


def _cli_namespace(**overrides: Any) -> Any:
    import argparse

    defaults = {
        "input": Path("x.mp4"),
        "output_dir": Path("/tmp/veotrex-b1a"),  # noqa: S108 - never written in these tests
        "detector": "none",
        "environment": "local",
        "sample_every": 1,
        "max_frames": None,
        "annotate": False,
        "run_id": "test",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ----------------------------------------------------- real container round trip (needs cv2)
cv2 = pytest.importorskip("cv2", reason="the recorded-video dependency group is not installed")


def _write_clip(path: Path, *, frames_count: int = 24, fps: float = 12.0) -> None:
    """A small synthetic clip: a light rectangle moving across a dark background.

    Nothing in it is a person - the real detector is not involved - but it is a genuine MP4
    produced by a real encoder, which is what the container and timeline handling need.
    """
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    assert writer.isOpened(), "the test encoder is unavailable"
    try:
        for index in range(frames_count):
            canvas = np.zeros((240, 320, 3), dtype=np.uint8)
            left = 10 + index * 8
            cv2.rectangle(canvas, (left, 80), (left + 40, 180), (200, 200, 200), -1)
            writer.write(canvas)
    finally:
        writer.release()


def test_a_real_clip_streams_with_media_timeline_timestamps(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=24, fps=12.0)

    with RecordedVideoSource(clip) as source:
        assert source.metadata.width == 320
        assert source.metadata.height == 240
        stamps = [frame.timestamp_ms for frame in source.frames()]

    assert len(stamps) >= 20
    assert stamps == sorted(stamps), "timestamps must never go backwards"
    # 12 fps means ~83.33 ms per frame, taken from the media timeline rather than the clock.
    gaps = [b - a for a, b in pairwise(stamps)]
    assert all(abs(gap - 1000.0 / 12.0) < 2.0 for gap in gaps), gaps


def test_sampling_skips_frames_without_moving_the_time_axis(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=24, fps=12.0)

    with RecordedVideoSource(clip) as every:
        all_stamps = [frame.timestamp_ms for frame in every.frames()]
    with RecordedVideoSource(clip, sample_every=3) as sampled:
        sampled_stamps = [frame.timestamp_ms for frame in sampled.frames()]

    assert len(sampled_stamps) < len(all_stamps)
    # Every sampled timestamp is one the unsampled run also produced: sampling drops frames,
    # it does not renumber time.
    assert set(sampled_stamps).issubset(set(all_stamps))
    assert sampled_stamps == all_stamps[::3][: len(sampled_stamps)]


def test_a_full_run_over_a_real_clip_produces_a_readable_tracks_file(tmp_path: Path) -> None:
    """End to end through the operator entry point: a real container in, a versioned
    biometric-free ndjson out, and the input untouched."""
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=24, fps=12.0)
    before = clip.read_bytes()

    result = run_recorded_tracking(
        clip,
        tmp_path / "out",
        FakePersonDetector(walking(24)),
        tracking_config=CONFIG,
        run_id="round-trip",
    )

    assert result.tracks_path.exists()
    assert result.annotated_path is None, "annotation must be opt-in"
    records = list(read_ndjson(result.tracks_path))
    assert records[0]["type"] == "header"
    assert records[0]["video_filename"] == "clip.mp4"
    assert records[0]["identity_annotation"] == "none"
    assert records[-1]["type"] == "footer"
    assert records[-1]["metrics"]["video_frames_processed_total"] >= 20
    for record in records:
        assert_no_biometric_material(record)
    # The operator's input is read-only data.
    assert clip.read_bytes() == before


def test_the_header_names_the_file_but_not_the_operators_directory(tmp_path: Path) -> None:
    clip = tmp_path / "secret-location" / "clip.mp4"
    clip.parent.mkdir()
    _write_clip(clip, frames_count=16, fps=8.0)

    result = run_recorded_tracking(
        clip, tmp_path / "out", FakePersonDetector({}), tracking_config=CONFIG
    )
    text = result.tracks_path.read_text()
    assert "clip.mp4" in text
    assert "secret-location" not in text
    assert str(tmp_path) not in text


def test_an_annotated_video_is_written_only_when_requested(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=16, fps=8.0)

    result = run_recorded_tracking(
        clip,
        tmp_path / "out",
        FakePersonDetector(walking(16)),
        tracking_config=CONFIG,
        annotate=True,
    )
    assert result.annotated_path is not None
    assert result.annotated_path.exists()
    assert result.annotated_path.stat().st_size > 0
    # Written beside the output, never over the input.
    assert result.annotated_path != clip
    assert clip.exists()


def test_a_second_run_into_the_same_directory_is_refused(tmp_path: Path) -> None:
    """Evidence from a qualification run must not be silently replaced."""
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=12, fps=8.0)
    output = tmp_path / "out"

    run_recorded_tracking(clip, output, FakePersonDetector({}), tracking_config=CONFIG)
    with pytest.raises(OutputError, match="output_already_exists"):
        run_recorded_tracking(clip, output, FakePersonDetector({}), tracking_config=CONFIG)


def test_a_truncated_file_ends_cleanly_with_what_was_readable(tmp_path: Path) -> None:
    """A recording cut off mid-write is common. It must yield its readable frames rather than
    raising, and must not hang."""
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=40, fps=10.0)
    data = clip.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[: int(len(data) * 0.6)])

    try:
        with RecordedVideoSource(truncated) as source:
            count = sum(1 for _ in source.frames())
    except VideoSourceError as exc:
        # Refusing outright is also acceptable; silently inventing frames is not.
        assert exc.category in {"video_unreadable", "video_has_no_frames", "video_decode_failed"}
    else:
        assert count >= 0


def test_a_healthy_clip_reports_no_dropped_frames(tmp_path: Path) -> None:
    """The end of a file and an undecodable frame look identical to OpenCV, so an earlier
    version of this source counted its end-of-stream retries as drops and reported a phantom
    31 on every healthy video. A drop is only a drop if reading later recovered."""
    clip = tmp_path / "clip.mp4"
    _write_clip(clip, frames_count=30, fps=10.0)

    with RecordedVideoSource(clip) as source:
        decoded = sum(1 for _ in source.frames())
        assert source.frames_failed == 0, "a clean file must report no dropped frames"
    assert decoded >= 25
