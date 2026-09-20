"""Pipeline wiring, with a stub detector so this runs without a GPU.

The stub stands in for TensorRT only. It returns detections in the same shape the real
detector emits, so what is under test is the wiring - source frame in, tracked occupancy and
an annotated frame out - not the model.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.frame_source.source import (
    FrameSourceError,
    SourceFrame,
    SourceHealth,
    SourceKind,
    SourceStatus,
)
from veotrex_edge_agent.monitoring.occupancy import (
    CoverageState,
    DemoStaffingPolicy,
    OccupancyCertainty,
)
from veotrex_edge_agent.monitoring.overlay import annotate
from veotrex_edge_agent.monitoring.pipeline import MonitoringPipeline
from veotrex_edge_agent.tracking import TrackState, TrackView

WIDTH, HEIGHT = 320, 240


class StubDetector:
    def __init__(self, boxes: list[tuple[float, float, float, float]]) -> None:
        self._boxes = boxes
        self.frame_ids: list[str] = []

    def infer_decoded(self, image: Any, **kwargs: Any) -> dict[str, Any]:
        self.frame_ids.append(kwargs["frame_id"])
        return {
            "detections": [
                {
                    "bbox_xyxy_source": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                    "score": 0.92,
                }
                for (x1, y1, x2, y2) in self._boxes
            ],
            "image_timing": {"decoded_frame_to_detection_ms": 12.5},
        }


class StubSource:
    kind = SourceKind.RECORDED_DEMO

    def __init__(self, frame_count: int, boxes_per_frame: int = 2) -> None:
        self._frame_count = frame_count
        self._boxes = boxes_per_frame
        self.health = SourceHealth.RUNNING
        self.started = False
        self.stopped = False
        self.restarts = 0

    def status(self) -> SourceStatus:
        return SourceStatus(self.kind, self.health, self._frame_count, 0, 0, None)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.health = SourceHealth.STOPPED

    def restart(self) -> None:
        self.restarts += 1

    def frames(self) -> Any:
        rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        for sequence in range(self._frame_count):
            yield SourceFrame(
                kind=self.kind,
                stream_instance_id="stub",
                sequence=sequence,
                media_timestamp_seconds=sequence * 0.2,
                monotonic_timestamp_seconds=sequence * 0.2,
                loop_index=0,
                width=WIDTH,
                height=HEIGHT,
                rgb=rgb,
                encoded_jpeg=b"",
            )


def run(frames: int, boxes: list[tuple[float, float, float, float]], **kwargs: Any):
    source = StubSource(frames)
    detector = StubDetector(boxes)
    clock = iter(float(index) * 0.2 for index in range(10_000))
    pipeline = MonitoringPipeline(
        source,  # type: ignore[arg-type]
        detector,
        area_label="Demo Classroom",
        camera_label="Demo Camera",
        clock=lambda: next(clock),
        **kwargs,
    )
    for frame in source.frames():
        pipeline._process(frame)
    return pipeline, source, detector


def test_detections_become_confirmed_tracks_and_a_measured_count() -> None:
    boxes = [(10.0, 10.0, 60.0, 170.0), (120.0, 10.0, 170.0, 170.0)]
    pipeline, _, detector = run(5, boxes)
    snapshot = pipeline.snapshot()
    assert snapshot.occupancy.certainty is OccupancyCertainty.MEASURED
    assert snapshot.occupancy.people_detected == 2
    assert snapshot.active_track_count == 2
    assert snapshot.frames_processed == 5
    assert detector.frame_ids[0] == "stub-0"


def test_telemetry_is_measured_from_the_detector_not_invented() -> None:
    pipeline, _, _ = run(4, [(10.0, 10.0, 60.0, 170.0)])
    snapshot = pipeline.snapshot()
    assert snapshot.inference_latency_ms == 12.5
    assert snapshot.measured_fps is not None


def test_a_recorded_source_never_reports_itself_as_live() -> None:
    pipeline, _, _ = run(3, [(10.0, 10.0, 60.0, 170.0)])
    assert pipeline.snapshot().source_kind is SourceKind.RECORDED_DEMO


def test_a_stalled_pipeline_degrades_to_impaired_on_its_own() -> None:
    """Coverage is recomputed when read, so a wedged worker cannot serve a stale green state."""
    source = StubSource(3)
    now = [0.0]
    pipeline = MonitoringPipeline(
        source,  # type: ignore[arg-type]
        StubDetector([(10.0, 10.0, 60.0, 170.0)]),
        area_label="Demo Classroom",
        camera_label="Demo Camera",
        stale_after_seconds=2.0,
        clock=lambda: now[0],
    )
    for frame in source.frames():
        now[0] += 0.2
        pipeline._process(frame)
    assert pipeline.snapshot().occupancy.coverage is CoverageState.ACTIVE
    now[0] += 30.0
    stalled = pipeline.snapshot()
    assert stalled.occupancy.coverage is CoverageState.IMPAIRED
    assert stalled.occupancy.people_detected is None
    assert stalled.active_track_count is None


def test_a_stopped_source_reports_unknown_not_empty() -> None:
    pipeline, source, _ = run(3, [(10.0, 10.0, 60.0, 170.0)])
    source.health = SourceHealth.STOPPED
    snapshot = pipeline.snapshot()
    assert snapshot.occupancy.coverage is CoverageState.UNKNOWN
    assert snapshot.occupancy.people_detected is None


def test_the_worker_thread_records_a_failure_instead_of_dying_silently() -> None:
    class ExplodingSource(StubSource):
        def frames(self) -> Any:
            raise FrameSourceError("decoder_failed")
            yield  # pragma: no cover

    source = ExplodingSource(0)
    pipeline = MonitoringPipeline(
        source,  # type: ignore[arg-type]
        StubDetector([]),
        area_label="Demo Classroom",
        camera_label="Demo Camera",
    )
    pipeline._run()
    assert pipeline.snapshot().source_error_category == "decoder_failed"


def test_threshold_policy_flows_through_to_the_snapshot() -> None:
    boxes = [(10.0 + index * 60, 10.0, 55.0 + index * 60, 170.0) for index in range(5)]
    pipeline, _, _ = run(5, boxes, policy=DemoStaffingPolicy(staff_on_duty=1, people_per_staff=2))
    snapshot = pipeline.snapshot()
    assert snapshot.occupancy.permitted_people == 2
    assert snapshot.occupancy.people_detected == 5
    assert str(snapshot.occupancy.threshold_state) == "OVER_THRESHOLD"


def test_restart_is_delegated_to_the_source() -> None:
    pipeline, source, _ = run(1, [])
    pipeline.restart_source()
    assert source.restarts == 1


def test_overlay_produces_a_jpeg_and_tolerates_out_of_frame_boxes() -> None:
    rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    track = TrackView(
        stream_instance_id="stub",
        track_id=7,
        state=TrackState.CONFIRMED,
        bbox_xyxy_source=(-50.0, -20.0, 10_000.0, 10_000.0),
        latest_detection_score=0.9,
        first_seen_timestamp=0.0,
        last_seen_timestamp=1.0,
        age_seconds=1.0,
        observation_count=3,
        consecutive_misses=0,
        was_low_score_recovery=False,
    )
    payload = annotate(rgb, (track,))
    assert payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")


@pytest.mark.parametrize("bad", [None, {}, {"score": 1.0}, {"bbox_xyxy_source": {}, "score": "x"}])
def test_malformed_detections_are_dropped_rather_than_crashing(bad: object) -> None:
    from veotrex_edge_agent.monitoring.pipeline import _detection

    assert _detection(bad) is None
