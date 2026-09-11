from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pytest

from veotrex_edge_agent.qualification.tracking_evaluator import (
    GroundTruthTrack,
    evaluate_synthetic_replay,
)
from veotrex_edge_agent.tracking.kalman import BoxKalmanFilter, KalmanStateError, xyxy_to_xyah
from veotrex_edge_agent.tracking.tracker import (
    PersonDetection,
    PersonTracker,
    TrackingConfig,
    TrackState,
    _associate,
    _Track,
    iou_matrix,
)


def detection(x: float, score: float = 0.9) -> PersonDetection:
    return PersonDetection((x, 10.0, x + 20.0, 50.0), score)


def update(
    tracker: PersonTracker,
    sequence: int,
    items: list[PersonDetection],
    *,
    timestamp: float | None = None,
    stream: str = "stream-a",
    width: int = 640,
    height: int = 480,
):
    return tracker.update(
        stream,
        sequence,
        float(sequence) / 10 if timestamp is None else timestamp,
        items,
        source_width=width,
        source_height=height,
    )


def confirmed_tracker(**changes: object) -> PersonTracker:
    return PersonTracker(TrackingConfig(confirmation_observations=1, **changes))


def test_kalman_initialize_predict_update() -> None:
    model = BoxKalmanFilter()
    mean, covariance = model.initiate(xyxy_to_xyah((10, 20, 30, 60)))
    predicted, predicted_covariance = model.predict(mean, covariance, 0.1)
    updated, updated_covariance = model.update(
        predicted, predicted_covariance, xyxy_to_xyah((11, 20, 31, 60))
    )
    assert updated.shape == (8,)
    assert updated_covariance.shape == (8, 8)
    assert updated[0] > mean[0]


@pytest.mark.parametrize("box", [(0, 0, 0, 2), (0, 0, 2, 0), (2, 0, 1, 2), (0, 2, 2, 1)])
def test_invalid_kalman_box(box: tuple[float, float, float, float]) -> None:
    with pytest.raises(KalmanStateError):
        xyxy_to_xyah(box)


def test_non_finite_kalman_state() -> None:
    model = BoxKalmanFilter()
    with pytest.raises(KalmanStateError, match="non_finite_state"):
        model.predict(np.full(8, np.nan), np.eye(8), 1.0)


def test_iou_matrix_and_assignment() -> None:
    model = BoxKalmanFilter()
    mean, covariance = model.initiate(xyxy_to_xyah((10, 10, 30, 50)))
    track = _Track(1, TrackState.CONFIRMED, mean, covariance, 0, 0, 0.9)
    matrix = iou_matrix([track], [detection(10), detection(100)])
    assert matrix.tolist() == [[1.0, 0.0]]
    assert _associate([track], [detection(10), detection(100)], 0.5) == ([(0, 0)], [], [1])


def test_exact_score_boundaries_and_low_cannot_create() -> None:
    tracker = confirmed_tracker()
    assert not update(tracker, 0, [detection(10, 0.05)]).confirmed_tracks
    result = update(tracker, 1, [detection(10, 0.30)])
    assert result.confirmed_tracks[0].track_id == 1
    result = update(tracker, 2, [detection(10, 0.05)])
    assert result.confirmed_tracks[0].was_low_score_recovery


def test_below_low_is_discarded() -> None:
    tracker = confirmed_tracker()
    assert not update(tracker, 0, [detection(10, 0.049)]).confirmed_tracks


def test_tentative_confirmation_suppresses_one_frame_false_positive() -> None:
    tracker = PersonTracker(TrackingConfig(confirmation_observations=2))
    first = update(tracker, 0, [detection(10)])
    assert len(first.tentative_tracks) == 1
    second = update(tracker, 1, [])
    assert second.removed_track_ids == (1,)
    assert not second.confirmed_tracks


def test_tentative_confirms_on_second_observation() -> None:
    tracker = PersonTracker()
    update(tracker, 0, [detection(10)])
    result = update(tracker, 1, [detection(11)])
    assert result.confirmed_tracks[0].track_id == 1


def test_lost_recovery_and_expiration() -> None:
    tracker = confirmed_tracker(max_lost_seconds=0.5)
    update(tracker, 0, [detection(10)])
    lost = update(tracker, 1, [])
    assert lost.lost_track_ids == (1,)
    recovered = update(tracker, 2, [detection(11)])
    assert recovered.recovered_track_ids == (1,)
    update(tracker, 3, [])
    removed = update(tracker, 10, [], timestamp=1.0)
    assert removed.removed_track_ids == (1,)
    replacement = update(tracker, 11, [detection(10)], timestamp=1.1)
    assert replacement.confirmed_tracks[0].track_id == 2


def test_missing_detection_uses_prediction() -> None:
    tracker = confirmed_tracker()
    update(tracker, 0, [detection(10)])
    update(tracker, 1, [detection(12)])
    update(tracker, 2, [])
    result = update(tracker, 3, [detection(16)])
    assert result.recovered_track_ids == (1,)


def test_two_streams_have_isolated_local_ids() -> None:
    tracker = confirmed_tracker()
    left = update(tracker, 0, [detection(10)], stream="one")
    right = update(tracker, 0, [detection(200)], stream="two")
    assert left.confirmed_tracks[0].track_id == right.confirmed_tracks[0].track_id == 1
    assert (
        left.confirmed_tracks[0].stream_instance_id != right.confirmed_tracks[0].stream_instance_id
    )


def test_sequence_and_timestamp_regression_rejected() -> None:
    tracker = confirmed_tracker()
    update(tracker, 1, [])
    with pytest.raises(ValueError, match="frame_sequence"):
        update(tracker, 1, [])
    with pytest.raises(ValueError, match="timestamp"):
        update(tracker, 2, [], timestamp=0.01)


def test_large_gap_is_explicit_discontinuity() -> None:
    tracker = confirmed_tracker(max_timestamp_gap_seconds=0.5)
    update(tracker, 0, [detection(10)], timestamp=0)
    result = update(tracker, 1, [detection(10)], timestamp=1)
    assert result.discontinuity
    assert result.confirmed_tracks[-1].track_id == 2


def test_resolution_change_resets_state() -> None:
    tracker = confirmed_tracker()
    update(tracker, 0, [detection(10)])
    result = update(tracker, 1, [detection(10)], width=800)
    assert result.discontinuity
    assert result.confirmed_tracks[0].track_id == 1


def test_stream_reset_starts_fresh_ephemeral_ids() -> None:
    tracker = confirmed_tracker()
    update(tracker, 0, [detection(10)])
    tracker.reset("stream-a")
    assert update(tracker, 0, [detection(10)]).confirmed_tracks[0].track_id == 1


def test_capacity_and_history_are_bounded() -> None:
    tracker = confirmed_tracker(max_active_tracks=2, max_history_samples=3)
    update(tracker, 0, [detection(10), detection(100), detection(200)])
    assert tracker.metrics.tracking_capacity_drops_total == 1
    for sequence in range(1, 8):
        update(tracker, sequence, [detection(10 + sequence), detection(100 + sequence)])
    stream = tracker._streams["stream-a"]
    assert max(len(track.history) for track in stream.tracks) == 3


def test_detection_limit_is_deterministic() -> None:
    tracker = confirmed_tracker(max_detections_per_frame=2)
    result = update(tracker, 0, [detection(100, 0.8), detection(10, 0.9), detection(200, 0.7)])
    assert result.rejected_detections == 1
    assert len(result.confirmed_tracks) == 2


def test_duplicate_track_suppression_is_deterministic() -> None:
    result = update(confirmed_tracker(), 0, [detection(10, 0.9), detection(10, 0.8)])
    assert [track.track_id for track in result.confirmed_tracks] == [1]
    assert result.removed_track_ids == (2,)


def test_stream_capacity_is_bounded() -> None:
    tracker = confirmed_tracker(max_streams=1)
    update(tracker, 0, [], stream="one")
    with pytest.raises(ValueError, match="stream_capacity"):
        update(tracker, 0, [], stream="two")


@pytest.mark.parametrize(
    "item",
    [
        PersonDetection((0, 0, 1, 1), float("nan")),
        PersonDetection((0, 0, float("inf"), 1), 0.9),
        PersonDetection((2, 0, 1, 1), 0.9),
        PersonDetection((-1, 0, 1, 1), 0.9),
        PersonDetection((0, 0, 641, 1), 0.9),
    ],
)
def test_malformed_detections_are_contained(item: PersonDetection) -> None:
    result = update(confirmed_tracker(), 0, [item])
    assert result.rejected_detections == 1
    assert not result.confirmed_tracks


def test_deterministic_replay_three_times() -> None:
    sequence = [[detection(10)], [detection(12)], [detection(14, 0.1)], [], [detection(18)]]
    outputs = []
    for _ in range(3):
        tracker = confirmed_tracker()
        outputs.append([asdict(update(tracker, i, items)) for i, items in enumerate(sequence)])
    for run in outputs:
        for frame in run:
            frame.pop("tracking_update_ms")
    assert outputs[0] == outputs[1] == outputs[2]


def test_low_score_association_retains_track_where_high_only_loses_it() -> None:
    two_stage = confirmed_tracker()
    high_only = confirmed_tracker(low_score_threshold=0.30)
    for tracker in (two_stage, high_only):
        update(tracker, 0, [detection(10, 0.9)])
    retained = update(two_stage, 1, [detection(11, 0.1)])
    lost = update(high_only, 1, [detection(11, 0.1)])
    assert retained.confirmed_tracks[0].track_id == 1
    assert lost.lost_track_ids == (1,)


def test_crossing_and_parallel_motion_are_deterministic() -> None:
    runs = []
    frames = [
        [detection(10), detection(100)],
        [detection(35), detection(75)],
        [detection(60), detection(50)],
    ]
    for _ in range(2):
        tracker = confirmed_tracker(first_max_association_cost=0.95)
        runs.append(
            [
                [x.track_id for x in update(tracker, i, items).confirmed_tracks]
                for i, items in enumerate(frames)
            ]
        )
    assert runs[0] == runs[1]


def test_empty_frames_and_metrics() -> None:
    tracker = confirmed_tracker()
    result = update(tracker, 0, [])
    assert not result.confirmed_tracks
    assert tracker.metrics.tracking_frames_total == 1


def test_synthetic_evaluator_reports_identity_switch_and_fragmentation() -> None:
    tracker = confirmed_tracker()
    frames = [
        update(tracker, 0, [detection(10)]),
        update(tracker, 1, []),
        update(tracker, 2, [detection(10)]),
    ]
    truths = [
        [GroundTruthTrack("person-a", detection(10).bbox_xyxy_source)],
        [GroundTruthTrack("person-a", detection(10).bbox_xyxy_source)],
        [GroundTruthTrack("person-a", detection(10).bbox_xyxy_source)],
    ]
    metrics = evaluate_synthetic_replay(truths, [list(x.confirmed_tracks) for x in frames])
    assert metrics.matched_gt_observations == 2
    assert metrics.missed_gt_observations == 1
    assert metrics.id_switches == 0
    assert metrics.track_fragmentations == 1
