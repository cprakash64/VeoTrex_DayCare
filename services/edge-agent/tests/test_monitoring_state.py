"""Occupancy, coverage and session-event behaviour.

These run everywhere: no GPU, no GStreamer, no video file. The properties under test are the
ones a childcare monitoring product must never get wrong.
"""

from __future__ import annotations

import pytest

from veotrex_edge_agent.frame_source.source import SourceHealth, SourceKind
from veotrex_edge_agent.monitoring.events import SafetyEventKind, SessionEventLog
from veotrex_edge_agent.monitoring.occupancy import (
    CoverageState,
    DemoStaffingPolicy,
    OccupancyCertainty,
    ThresholdState,
    coverage_for,
    read_occupancy,
)
from veotrex_edge_agent.monitoring.pipeline import PipelineSnapshot
from veotrex_edge_agent.monitoring.server import snapshot_payload
from veotrex_edge_agent.tracking import PersonDetection, PersonTracker


def tracked(count: int) -> object:
    """Drive the real tracker to CONFIRMED so the count under test is genuinely its output."""
    tracker = PersonTracker()
    detections = [
        PersonDetection((10.0 + index * 80, 10.0, 60.0 + index * 80, 170.0), 0.9)
        for index in range(count)
    ]
    result = None
    for sequence in range(4):
        result = tracker.update(
            "stream", sequence, sequence * 0.2, detections, source_width=640, source_height=480
        )
    return result


def test_occupancy_counts_confirmed_tracks_only() -> None:
    result = tracked(3)
    reading = read_occupancy(result, coverage=CoverageState.ACTIVE, observed_at_monotonic=1.0)
    assert reading.people_detected == 3
    assert reading.certainty is OccupancyCertainty.MEASURED
    assert len(reading.confirmed_track_ids) == 3


def test_a_brand_new_tentative_track_is_not_counted_yet() -> None:
    tracker = PersonTracker()
    first = tracker.update(
        "stream",
        0,
        0.0,
        [PersonDetection((10.0, 10.0, 60.0, 170.0), 0.9)],
        source_width=640,
        source_height=480,
    )
    assert first.tentative_tracks and not first.confirmed_tracks
    reading = read_occupancy(first, coverage=CoverageState.ACTIVE, observed_at_monotonic=0.0)
    assert reading.people_detected == 0


@pytest.mark.parametrize("coverage", [CoverageState.IMPAIRED, CoverageState.UNKNOWN])
def test_lost_coverage_is_unknown_occupancy_and_never_zero(coverage: CoverageState) -> None:
    """The property that matters most: no video is not evidence of an empty room."""
    reading = read_occupancy(tracked(4), coverage=coverage, observed_at_monotonic=9.0)
    assert reading.people_detected is None
    assert reading.certainty is OccupancyCertainty.UNKNOWN
    assert reading.confirmed_track_ids == ()


def test_coverage_requires_a_running_source_and_a_recent_frame() -> None:
    assert (
        coverage_for(source_running=True, seconds_since_last_frame=0.2, stale_after_seconds=3.0)
        is CoverageState.ACTIVE
    )
    assert (
        coverage_for(source_running=True, seconds_since_last_frame=9.0, stale_after_seconds=3.0)
        is CoverageState.IMPAIRED
    )
    assert (
        coverage_for(source_running=False, seconds_since_last_frame=0.1, stale_after_seconds=3.0)
        is CoverageState.UNKNOWN
    )
    assert (
        coverage_for(source_running=True, seconds_since_last_frame=None, stale_after_seconds=3.0)
        is CoverageState.UNKNOWN
    )


def test_threshold_is_not_configured_without_declared_staff() -> None:
    reading = read_occupancy(tracked(2), coverage=CoverageState.ACTIVE, observed_at_monotonic=1.0)
    assert reading.threshold_state is ThresholdState.NOT_CONFIGURED
    assert reading.permitted_people is None


def test_threshold_compares_head_count_against_declared_capacity() -> None:
    policy = DemoStaffingPolicy(staff_on_duty=1, people_per_staff=4)
    within = read_occupancy(
        tracked(3), coverage=CoverageState.ACTIVE, observed_at_monotonic=1.0, policy=policy
    )
    assert within.threshold_state is ThresholdState.WITHIN_THRESHOLD
    assert within.permitted_people == 4
    over = read_occupancy(
        tracked(5), coverage=CoverageState.ACTIVE, observed_at_monotonic=2.0, policy=policy
    )
    assert over.threshold_state is ThresholdState.OVER_THRESHOLD


def test_threshold_is_unknown_rather_than_compliant_when_coverage_is_lost() -> None:
    policy = DemoStaffingPolicy(staff_on_duty=1, people_per_staff=4)
    reading = read_occupancy(
        tracked(2), coverage=CoverageState.UNKNOWN, observed_at_monotonic=3.0, policy=policy
    )
    assert reading.threshold_state is ThresholdState.UNKNOWN


def test_staffing_policy_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="positive"):
        DemoStaffingPolicy(staff_on_duty=0, people_per_staff=4)


def test_events_are_only_emitted_on_real_transitions() -> None:
    log = SessionEventLog("Demo Classroom")
    policy = DemoStaffingPolicy(staff_on_duty=1, people_per_staff=4)
    active = read_occupancy(
        tracked(2), coverage=CoverageState.ACTIVE, observed_at_monotonic=1.0, policy=policy
    )
    assert log.observe(active) == ()  # first observation establishes a baseline
    assert log.observe(active) == ()  # steady state emits nothing
    assert log.events() == ()

    lost = read_occupancy(
        None, coverage=CoverageState.UNKNOWN, observed_at_monotonic=5.0, policy=policy
    )
    emitted = log.observe(lost)
    assert [event.kind for event in emitted] == [SafetyEventKind.MONITORING_COVERAGE_LOST]

    restored = read_occupancy(
        tracked(2), coverage=CoverageState.ACTIVE, observed_at_monotonic=12.0, policy=policy
    )
    emitted = log.observe(restored)
    assert [event.kind for event in emitted] == [SafetyEventKind.MONITORING_COVERAGE_RESTORED]
    assert emitted[0].duration_seconds == 7.0


def test_threshold_events_carry_the_duration_they_lasted() -> None:
    log = SessionEventLog("Demo Classroom")
    policy = DemoStaffingPolicy(staff_on_duty=1, people_per_staff=2)
    log.observe(
        read_occupancy(
            tracked(1), coverage=CoverageState.ACTIVE, observed_at_monotonic=0.0, policy=policy
        )
    )
    exceeded = log.observe(
        read_occupancy(
            tracked(5), coverage=CoverageState.ACTIVE, observed_at_monotonic=4.0, policy=policy
        )
    )
    assert [event.kind for event in exceeded] == [SafetyEventKind.DEMO_THRESHOLD_EXCEEDED]
    cleared = log.observe(
        read_occupancy(
            tracked(1), coverage=CoverageState.ACTIVE, observed_at_monotonic=16.0, policy=policy
        )
    )
    assert [event.kind for event in cleared] == [SafetyEventKind.DEMO_THRESHOLD_CLEARED]
    assert cleared[0].duration_seconds == 12.0


def test_event_log_is_bounded() -> None:
    log = SessionEventLog("Demo Classroom", max_events=4)
    for index in range(40):
        coverage = CoverageState.ACTIVE if index % 2 else CoverageState.UNKNOWN
        log.observe(read_occupancy(tracked(1), coverage=coverage, observed_at_monotonic=index))
    assert len(log.events()) == 4


def snapshot(**overrides: object) -> PipelineSnapshot:
    base = {
        "source_kind": SourceKind.RECORDED_DEMO,
        "source_health": SourceHealth.RUNNING,
        "area_label": "Demo Classroom",
        "camera_label": "Demo Camera",
        "occupancy": read_occupancy(
            tracked(2), coverage=CoverageState.ACTIVE, observed_at_monotonic=1.0
        ),
        "active_track_count": 2,
        "measured_fps": None,
        "inference_latency_ms": None,
        "frames_processed": 10,
        "loops_completed": 0,
        "decode_failures": 0,
        "seconds_since_last_frame": 0.1,
        "media_timestamp_seconds": 2.0,
        "source_error_category": None,
        "peak_occupancy": 2,
        "tracks_observed": 2,
        "longest_track_seconds": 0.6,
        "session_seconds": 1.0,
        "events": (),
    }
    return PipelineSnapshot(**(base | overrides))  # type: ignore[arg-type]


def test_payload_marks_a_recording_as_not_live() -> None:
    payload = snapshot_payload(snapshot())
    assert payload["source"]["kind"] == "RECORDED_DEMO"
    assert payload["source"]["is_live"] is False


def test_payload_publishes_null_for_metrics_that_were_never_measured() -> None:
    """An un-measured metric must reach the UI as absent, never as a plausible-looking 0."""
    payload = snapshot_payload(snapshot())
    assert payload["telemetry"]["measured_fps"] is None
    assert payload["telemetry"]["inference_latency_ms"] is None
