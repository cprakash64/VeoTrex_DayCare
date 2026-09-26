"""Occupancy integrity and camera nuisance calibration (V1-03B).

Every scene here is scripted: generated NumPy frames and detections a test chose, including a
reproduction of the real nuisance (a static ~73x305 px box scoring 0.07-0.32). No camera, no
model, no recording, and nothing from ``data/``.

The safety rule these tests defend: stillness is never evidence that something is not a person.
A person who does not move must be counted exactly like one who does.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from veotrex_edge_agent.live import (
    OCCUPANCY_CANDIDATE,
    OCCUPANCY_VALIDATED,
    PERSISTENT_LOW_CONFIDENCE_CANDIDATE,
    DemoEventKind,
    FakeLiveSource,
    InferenceRateConfig,
    LiveDemoRuntime,
    LiveFrame,
    OccupancyEvidencePolicy,
    OccupancyLedger,
)
from veotrex_edge_agent.live import occupancy as occupancy_module
from veotrex_edge_agent.live.server import DemoServer
from veotrex_edge_agent.recorded.model import DetectedPerson
from veotrex_edge_agent.recorded.regions import (
    MAX_IGNORE_REGIONS,
    IgnoreRegion,
    IgnoreRegionError,
    IgnoreRegionSet,
    build_ignore_regions,
    parse_ignore_region,
)
from veotrex_edge_agent.tracking import PersonDetection, PersonTracker, TrackingConfig

WIDTH, HEIGHT = 320, 240
FPS = 16.0
CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
# A nuisance scaled from the real one (73x305 at 1280x720) into this 320x240 frame.
NUISANCE_BOX = (237.0, 63.0, 255.0, 165.0)
PERSON_BOX = (40.0, 30.0, 110.0, 230.0)

Script = Callable[[int], list[tuple[tuple[float, float, float, float], float]]]


class ScriptedDetector:
    """Returns whatever ``script(frame_index)`` says, optionally after a real delay."""

    model_id = "scripted"
    model_version = "1"

    def __init__(self, script: Script, *, latency: float = 0.0) -> None:
        self._script = script
        self._latency = latency
        self.calls = 0

    def detect(self, image: Any, *, frame_index: int, timestamp_ms: float) -> list[DetectedPerson]:
        self.calls += 1
        if self._latency:
            time.sleep(self._latency)
        return [
            DetectedPerson(box, score, frame_index, timestamp_ms)
            for box, score in self._script(frame_index)
        ]


def nuisance_score(index: int) -> float:
    """Two high frames to be born and confirmed, then a low band that never reaches 0.30."""
    if index < 2:
        return (0.31, 0.32)[index]
    return (0.07, 0.12, 0.2, 0.29, 0.15, 0.1)[index % 6]


def moved(box: tuple[float, float, float, float], dx: float) -> tuple[float, float, float, float]:
    return (box[0] + dx, box[1], box[2] + dx, box[3])


def run(
    script: Script,
    *,
    frames: int,
    regions: IgnoreRegionSet | None = None,
    source: FakeLiveSource | None = None,
    **runtime_kwargs: Any,
) -> LiveDemoRuntime:
    # Slow enough that the unpaced consumer processes every frame even on a loaded machine:
    # several assertions below (a nuisance born on two consecutive frames, every detection
    # reaching the tracker) depend on no frame being superseded.
    source = source or FakeLiveSource(
        frame_count=frames, width=WIDTH, height=HEIGHT, fps=FPS, interval_seconds=0.008
    )
    runtime = LiveDemoRuntime(
        source,
        ScriptedDetector(script),
        tracking_config=CONFIG,
        ignore_regions=regions,
        **runtime_kwargs,
    )
    runtime.run()
    return runtime


def event_values(runtime: LiveDemoRuntime, kind: DemoEventKind, key: str) -> list[Any]:
    return [event[key] for event in reversed(runtime.timeline.recent(200)) if event["kind"] == kind]


def diagnostics_for(runtime: LiveDemoRuntime, track_id: int) -> dict[str, Any]:
    report = runtime.occupancy_diagnostics()
    for entry in report["live"] + report["recently_ended"]:
        if entry["track_id"] == track_id:
            found: dict[str, Any] = entry
            return found
    raise AssertionError(f"no diagnostics for track {track_id}")


def track(tracker: PersonTracker, sequence: int, scores: list[float | None]) -> Any:
    result = None
    for index, score in enumerate(scores, start=sequence):
        detections = [] if score is None else [PersonDetection(NUISANCE_BOX, score)]
        result = tracker.update(
            "s", index, index * 0.17, detections, source_width=WIDTH, source_height=HEIGHT
        )
    return result


# ============================================================== tracker contract (ADR 0012)
def test_low_score_detections_cannot_birth_a_track() -> None:
    tracker = PersonTracker(TrackingConfig())
    for index, score in enumerate([0.29, 0.2, 0.07, 0.05, 0.29, 0.25, 0.1]):
        result = tracker.update(
            "s",
            index,
            index * 0.17,
            [PersonDetection(NUISANCE_BOX, score)],
            source_width=WIDTH,
            source_height=HEIGHT,
        )
        assert not result.confirmed_tracks and not result.tentative_tracks
    assert tracker.metrics.tracking_tracks_created_total == 0


def test_low_score_detections_cannot_confirm_a_tentative_track() -> None:
    tracker = PersonTracker(TrackingConfig())
    result = track(tracker, 0, [0.31, 0.2])
    assert not result.confirmed_tracks, "a tentative track is confirmed only by high scores"


def test_low_score_detections_maintain_an_established_track_as_documented() -> None:
    tracker = PersonTracker(TrackingConfig())
    result = track(tracker, 0, [0.9, 0.9, 0.2, 0.1, 0.07, 0.2])
    assert [view.track_id for view in result.confirmed_tracks] == [1]
    assert result.confirmed_tracks[0].was_low_score_recovery


def test_a_lost_track_recovers_only_on_a_high_score_detection() -> None:
    tracker = PersonTracker(TrackingConfig())
    after_low = track(tracker, 0, [0.9, 0.9, None, 0.2])
    assert not after_low.confirmed_tracks, "LOST tracks do not take low-score associations"
    recovered = track(tracker, 4, [0.9])
    assert [view.track_id for view in recovered.confirmed_tracks] == [1]


def test_the_configured_confirmation_count_is_honoured() -> None:
    """Regression: every match used to promote a TENTATIVE track, so a configured count above
    two was silently ignored."""
    tracker = PersonTracker(TrackingConfig(confirmation_observations=3))
    second = track(tracker, 0, [0.9, 0.9])
    assert not second.confirmed_tracks and second.tentative_tracks
    third = track(tracker, 2, [0.9])
    assert [view.track_id for view in third.confirmed_tracks] == [1]
    assert tracker.metrics.tracking_tracks_confirmed_total == 1


def test_default_confirmation_behaviour_is_unchanged() -> None:
    tracker = PersonTracker(TrackingConfig())
    assert not track(tracker, 0, [0.9]).confirmed_tracks
    assert [view.track_id for view in track(tracker, 1, [0.9]).confirmed_tracks] == [1]


def test_no_tracker_threshold_was_changed() -> None:
    config = TrackingConfig()
    assert (config.low_score_threshold, config.high_score_threshold) == (0.05, 0.30)
    assert config.new_track_threshold == 0.30
    assert config.confirmation_observations == 2
    assert config.max_lost_seconds == 2.0


# ========================================================================= occupancy rules
def test_a_low_score_only_sequence_never_reaches_occupancy() -> None:
    runtime = run(lambda i: [(NUISANCE_BOX, 0.07 + (i % 5) * 0.05)], frames=40)
    metrics = runtime.metrics()
    assert runtime.timeline.peak_occupancy == 0
    assert metrics["tracks_created_total"] == 0
    assert metrics["occupancy_candidate_tracks_total"] == 0


def test_a_stationary_high_confidence_person_is_counted_and_stays_counted() -> None:
    """The safety rule: a person who does not move at all is still a person."""
    runtime = run(lambda _i: [(PERSON_BOX, 0.88)], frames=60)
    assert event_values(runtime, DemoEventKind.OCCUPANCY_CHANGED, "occupancy") == [1, 0]
    assert event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id") == [1]
    diagnostics = diagnostics_for(runtime, 1)
    assert diagnostics["status"] == OCCUPANCY_VALIDATED
    # Zero spread, and still validated: stillness is not evidence of anything.
    assert diagnostics["centre_normalized"]["spread_x"] == 0.0
    assert diagnostics["centre_normalized"]["spread_y"] == 0.0
    assert diagnostics["labels"] == []


def test_a_moving_high_confidence_person_is_counted() -> None:
    runtime = run(lambda i: [(moved(PERSON_BOX, i * 2.0), 0.9)], frames=50)
    assert event_values(runtime, DemoEventKind.OCCUPANCY_CHANGED, "occupancy") == [1, 0]
    assert diagnostics_for(runtime, 1)["status"] == OCCUPANCY_VALIDATED


def test_a_person_is_validated_one_observation_after_confirmation() -> None:
    """Two high observations confirm (tracker); one more high observation validates."""
    ledger = OccupancyLedger(OccupancyEvidencePolicy.from_tracking(CONFIG))
    ledger.start(1, prior_high_observations=1)
    first = ledger.observe(
        1, score=0.9, bbox=PERSON_BOX, width=WIDTH, height=HEIGHT, timestamp_ms=0.0
    )
    assert first is False
    assert ledger.status(1) == OCCUPANCY_CANDIDATE
    second = ledger.observe(
        1, score=0.9, bbox=PERSON_BOX, width=WIDTH, height=HEIGHT, timestamp_ms=60.0
    )
    assert second is True
    assert ledger.status(1) == OCCUPANCY_VALIDATED
    assert ledger.snapshot()["occupancy_candidate_to_validated_total"] == 1


def test_temporary_confidence_degradation_does_not_flap_occupancy() -> None:
    def script(index: int) -> list[Any]:
        score = 0.9 if index < 12 or index >= 30 else 0.12
        return [(PERSON_BOX, score)]

    runtime = run(script, frames=45)
    assert event_values(runtime, DemoEventKind.OCCUPANCY_CHANGED, "occupancy") == [1, 0]
    assert len(event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id")) == 1


def test_a_confirmed_person_stays_counted_through_scheduler_gaps() -> None:
    source = FakeLiveSource(
        frame_count=40, width=WIDTH, height=HEIGHT, fps=FPS, interval_seconds=1.0 / FPS
    )
    runtime = LiveDemoRuntime(
        source,
        ScriptedDetector(lambda i: [(moved(PERSON_BOX, i * 1.5), 0.85)], latency=0.08),
        tracking_config=CONFIG,
        inference_rate=InferenceRateConfig(),
    )
    runtime.run()
    assert runtime.metrics()["inference_frames_skipped_scheduler_total"] > 0
    assert event_values(runtime, DemoEventKind.OCCUPANCY_CHANGED, "occupancy") == [1, 0]
    assert event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id") == [1]


def test_a_real_disappearance_still_clears_occupancy_after_the_tolerance() -> None:
    runtime = run(lambda i: [(PERSON_BOX, 0.9)] if i < 16 else [], frames=48)
    kinds = [event["kind"] for event in reversed(runtime.timeline.recent(200))]
    gone = kinds.index("PERSON_NO_LONGER_VISIBLE")
    assert gone < kinds.index("TRACKING_STOPPED")
    assert runtime.timeline.occupancy == 0


def test_a_reconnect_still_ends_old_tracks() -> None:
    class Interrupted(FakeLiveSource):
        def frames(self) -> Iterator[LiveFrame]:
            for frame in super().frames():
                yield dataclasses.replace(frame, discontinuity=frame.frame_index == 20)

    source = Interrupted(
        frame_count=40, width=WIDTH, height=HEIGHT, fps=FPS, interval_seconds=0.004
    )
    runtime = run(lambda _i: [(PERSON_BOX, 0.9)], frames=40, source=source)
    assert event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id") == [1, 2]
    assert event_values(runtime, DemoEventKind.PERSON_NO_LONGER_VISIBLE, "track_id")[0] == 1
    assert runtime.timeline.peak_occupancy == 1


# ===================================================================== nuisance, not hidden
def nuisance_run(frames: int = 120, **kwargs: Any) -> LiveDemoRuntime:
    return run(lambda i: [(NUISANCE_BOX, nuisance_score(i))], frames=frames, **kwargs)


def test_the_real_nuisance_pattern_is_a_candidate_not_an_occupant() -> None:
    runtime = nuisance_run()
    metrics = runtime.metrics()
    assert runtime.timeline.peak_occupancy == 0, "a 0.07-0.32 static box must not be counted"
    assert metrics["tracks_created_total"] == 1, "but the tracker still follows it"
    assert event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id") == []
    assert metrics["occupancy_candidate_tracks_total"] == 1
    assert metrics["occupancy_candidate_to_validated_total"] == 0
    assert metrics["occupancy_candidates_ended_unvalidated_total"] == 1


def test_a_candidate_is_shown_while_live_and_labelled_for_review() -> None:
    source = FakeLiveSource(
        frame_count=10_000, width=WIDTH, height=HEIGHT, fps=FPS, interval_seconds=0.002
    )
    runtime = LiveDemoRuntime(
        source,
        ScriptedDetector(lambda i: [(NUISANCE_BOX, nuisance_score(i))]),
        tracking_config=CONFIG,
    )
    runtime.start()
    try:
        deadline = time.monotonic() + 10.0
        flagged: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            flagged = [
                entry
                for entry in runtime.occupancy_diagnostics()["live"]
                if PERSISTENT_LOW_CONFIDENCE_CANDIDATE in entry["labels"]
            ]
            if flagged:
                break
            time.sleep(0.02)
        state = runtime.state.as_dict()
    finally:
        runtime.stop()
    assert flagged, "a persistent candidate must be flagged for review"
    assert state["occupancy"] == 0
    assert state["candidate_tracks"] == 1
    assert [box["occupancy_status"] for box in state["tracks"]] in ([], [OCCUPANCY_CANDIDATE])
    entry = flagged[0]
    assert entry["status"] == OCCUPANCY_CANDIDATE
    assert entry["confidence"]["max"] <= 0.32
    envelope = entry["bbox_envelope_normalized"]
    assert envelope == pytest.approx(
        [
            NUISANCE_BOX[0] / WIDTH,
            NUISANCE_BOX[1] / HEIGHT,
            NUISANCE_BOX[2] / WIDTH,
            NUISANCE_BOX[3] / HEIGHT,
        ],
        abs=0.01,
    )
    suggestion = entry["suggested_ignore_region"]
    assert suggestion["applied"] is False and suggestion["requires_operator_review"] is True
    region = IgnoreRegion(suggestion["x1"], suggestion["y1"], suggestion["x2"], suggestion["y2"])
    assert region.containment_of(NUISANCE_BOX, width=WIDTH, height=HEIGHT) == pytest.approx(1.0)
    # A person standing in front of the suggested region would not be suppressed by it.
    in_front = (200.0, 20.0, 290.0, 235.0)
    assert region.containment_of(in_front, width=WIDTH, height=HEIGHT) < 0.8


def test_the_nuisance_diagnostic_never_suppresses_anything() -> None:
    runtime = nuisance_run()
    metrics = runtime.metrics()
    assert metrics["detections_ignored_total"] == 0
    assert metrics["ignore_regions_configured"] == 0
    assert metrics["person_detections_total"] == 120, "every detection still reached the tracker"
    assert runtime.calibration()["count"] == 0


def test_a_moving_low_confidence_track_is_not_validated_for_moving() -> None:
    runtime = run(lambda i: [(moved(NUISANCE_BOX, -i * 0.5), nuisance_score(i))], frames=100)
    assert runtime.timeline.peak_occupancy == 0
    assert runtime.metrics()["occupancy_candidate_to_validated_total"] == 0


def test_a_weak_but_real_person_is_counted_once_the_evidence_is_there() -> None:
    """A distant or partly occluded person scoring mostly low, with some clear frames, is not
    discarded: they are a candidate first and are counted once the evidence arrives."""

    def script(index: int) -> list[Any]:
        return [(PERSON_BOX, 0.35 if index % 3 == 0 or index < 2 else 0.18)]

    runtime = run(script, frames=40)
    assert runtime.timeline.peak_occupancy == 1
    assert diagnostics_for(runtime, 1)["status"] == OCCUPANCY_VALIDATED


def test_a_real_person_beside_the_nuisance_is_counted_alone() -> None:
    runtime = run(lambda i: [(PERSON_BOX, 0.9), (NUISANCE_BOX, nuisance_score(i))], frames=80)
    assert runtime.timeline.peak_occupancy == 1
    assert event_values(runtime, DemoEventKind.PERSON_APPEARED_IN_VIEW, "track_id") == [1]


def test_ledger_memory_is_bounded() -> None:
    ledger = OccupancyLedger()
    for track_id in range(occupancy_module.MAX_TRACKED_EVIDENCE + 50):
        ledger.start(track_id)
    assert ledger.metrics.evidence_capacity_refusals_total == 50
    for track_id in range(200):
        ledger.end(track_id)
    assert (
        len(ledger.diagnostics()["recently_ended"])
        == occupancy_module.FINISHED_DIAGNOSTICS_CAPACITY
    )
    ledger.start(10_000)
    for index in range(5000):
        ledger.observe(
            10_000, score=0.2, bbox=PERSON_BOX, width=WIDTH, height=HEIGHT, timestamp_ms=index
        )
    evidence = ledger._live[10_000]
    assert len(evidence.window) == ledger.policy.evidence_window
    assert len(evidence.recent_scores) == occupancy_module.RECENT_SCORE_CAPACITY


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_high_observations": 0},
        {"min_high_observations": 11, "evidence_window": 10},
        {"evidence_window": 65, "min_high_observations": 3},
        {"high_score_threshold": 0.0},
        {"suggestion_margin": 0.9},
    ],
)
def test_an_invalid_occupancy_policy_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OccupancyEvidencePolicy(**overrides)


def test_the_policy_uses_the_trackers_own_high_threshold() -> None:
    policy = OccupancyEvidencePolicy.from_tracking(
        TrackingConfig(high_score_threshold=0.4, new_track_threshold=0.4)
    )
    assert policy.high_score_threshold == 0.4


# ============================================================================ ignore regions
def test_an_ignore_region_suppresses_the_nuisance_before_tracking() -> None:
    regions = build_ignore_regions(["0.72,0.24,0.82,0.72,shelf edge"])
    runtime = nuisance_run(frames=60, regions=regions)
    metrics = runtime.metrics()
    assert metrics["tracks_created_total"] == 0
    assert metrics["occupancy_candidate_tracks_total"] == 0
    assert metrics["detections_ignored_total"] == 60
    assert runtime.calibration()["detections_suppressed_total"] == 60


def test_partial_containment_follows_the_configured_rule() -> None:
    region = IgnoreRegion(0.0, 0.0, 0.5, 0.6)
    box = [0.0, 0.0, 50.0, 100.0]  # exactly 60 % inside
    assert (
        IgnoreRegionSet((region,), min_containment=0.8).matching(box, width=100, height=100) is None
    )
    assert IgnoreRegionSet((region,), min_containment=0.6).matching(box, width=100, height=100)


def test_a_person_in_front_of_a_masked_nuisance_is_still_counted() -> None:
    regions = build_ignore_regions(["0.72,0.24,0.82,0.72,shelf edge"])
    in_front = (200.0, 20.0, 290.0, 235.0)  # overlaps the region, mostly outside it
    runtime = run(
        lambda i: [(in_front, 0.9), (NUISANCE_BOX, nuisance_score(i))], frames=40, regions=regions
    )
    assert runtime.timeline.peak_occupancy == 1
    assert runtime.metrics()["detections_ignored_total"] == 40, "only the nuisance was masked"


@pytest.mark.parametrize(
    "text",
    [
        "0.1,0.1,0.2",  # too few values
        "a,0.1,0.2,0.2",  # not a number
        "0.3,0.1,0.2,0.2",  # inverted x
        "0.1,0.3,0.2,0.2",  # inverted y
        "0.1,0.1,0.1,0.2",  # zero width
        "nan,0.1,0.2,0.2",
        "0.1,inf,0.2,0.2",
        "-0.1,0.1,0.2,0.2",  # outside the frame
        "0.1,0.1,1.2,0.2",
        "0.1,0.1,0.102,0.2",  # narrower than the minimum region side
        "0.1,0.1,0.2,0.2,<script>",  # markup in a label
        "0.1,0.1,0.2,0.2," + "x" * 41,  # label too long
    ],
)
def test_an_unacceptable_region_is_refused(text: str) -> None:
    with pytest.raises(IgnoreRegionError):
        parse_ignore_region(text)


def test_a_boolean_or_non_finite_coordinate_is_refused() -> None:
    with pytest.raises(IgnoreRegionError):
        IgnoreRegion(True, 0.1, 0.2, 0.2)
    with pytest.raises(IgnoreRegionError):
        IgnoreRegion(0.1, math.inf, 0.2, 0.2)
    with pytest.raises(IgnoreRegionError):
        IgnoreRegionSet((IgnoreRegion(0.1, 0.1, 0.2, 0.2),), min_containment=math.nan)


def test_too_many_regions_are_refused() -> None:
    regions = [f"{i / 10:.1f},0.1,{i / 10 + 0.05:.2f},0.2" for i in range(MAX_IGNORE_REGIONS + 1)]
    with pytest.raises(IgnoreRegionError, match="at most"):
        build_ignore_regions(regions)


@pytest.mark.parametrize(
    "texts",
    [["0,0,1,1"], ["0.0,0.0,0.75,0.75"], ["0,0,0.5,0.9", "0.5,0,1,0.9"]],
)
def test_whole_or_near_whole_frame_exclusion_fails_closed(texts: list[str]) -> None:
    with pytest.raises(IgnoreRegionError):
        build_ignore_regions(texts)


def test_a_low_containment_rule_is_flagged_everywhere_it_is_shown() -> None:
    regions = build_ignore_regions(["0.72,0.24,0.82,0.72,shelf"], min_containment=0.3)
    assert regions.low_containment
    assert regions.status()["low_containment_warning"] is True
    assert not build_ignore_regions(["0.72,0.24,0.82,0.72"]).low_containment


# ================================================================================ dashboard
def test_the_dashboard_shows_calibration_and_distinguishes_candidates() -> None:
    regions = build_ignore_regions(["0.05,0.05,0.15,0.15,poster by door"])
    source = FakeLiveSource(
        frame_count=10_000, width=WIDTH, height=HEIGHT, fps=FPS, interval_seconds=0.004
    )
    runtime = LiveDemoRuntime(
        source,
        ScriptedDetector(lambda i: [(PERSON_BOX, 0.9), (NUISANCE_BOX, nuisance_score(i))]),
        tracking_config=CONFIG,
        ignore_regions=regions,
    )
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        payload: dict[str, Any] = {}
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5) as reply:
                payload = json.loads(reply.read())
            statuses = {t["occupancy_status"] for t in payload["state"]["tracks"]}
            if statuses == {OCCUPANCY_VALIDATED, OCCUPANCY_CANDIDATE}:
                break
            time.sleep(0.05)
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
        runtime.stop()
    state = payload["state"]
    assert state["occupancy"] == 1 and state["candidate_tracks"] == 1
    assert {t["occupancy_status"] for t in state["tracks"]} == {
        OCCUPANCY_VALIDATED,
        OCCUPANCY_CANDIDATE,
    }
    calibration = payload["calibration"]
    assert calibration["count"] == 1
    assert calibration["regions"][0]["label"] == "poster by door"
    assert calibration["min_containment"] == 0.8
    assert "occupancy_diagnostics" in payload
    for text in (
        "People currently visible",
        "counted in occupancy",
        "Candidate person tracks",
        "Camera calibration",
        "Detections suppressed",
        "never masked automatically",
    ):
        assert text in page, text
    # Operator text reaches the page through textContent only.
    assert "li.textContent = text" in page


def test_no_identity_or_biometric_state_is_introduced() -> None:
    runtime = nuisance_run(frames=40)
    rendered = json.dumps(
        {
            "diagnostics": runtime.occupancy_diagnostics(),
            "state": runtime.state.as_dict(),
            "calibration": runtime.calibration(),
            "metrics": runtime.metrics(),
        }
    ).lower()
    for forbidden in (
        "identity",
        "face",
        "embedding",
        "staff",
        "child",
        "teacher",
        "gender",
        "name",
    ):
        assert forbidden not in rendered, forbidden
    text = Path(occupancy_module.__file__ or "").read_text(encoding="utf-8").lower()
    for forbidden in ("face_backend", "face_matching", "sface", "yunet", "cv2"):
        assert forbidden not in text, forbidden


def test_the_dashboard_classroom_card_never_counts_people_by_role() -> None:
    """V1-04A: the live dashboard has no presence source; it must say so, and must present the
    camera's head count as people seen - never a role count or a ratio input - while keeping the
    page free of demographic words (asserted by the existing presentation tests)."""
    from veotrex_edge_agent.live.server import PAGE

    assert "Presence counts not connected" in PAGE
    assert "People seen by camera (not a presence record)" in PAGE
    lowered = PAGE.lower()
    for claim in ("ratio: ", "required staff", "compliant", "legal", "staff count"):
        assert claim not in lowered, claim
    assert "local evaluation &mdash; no identification" in PAGE
