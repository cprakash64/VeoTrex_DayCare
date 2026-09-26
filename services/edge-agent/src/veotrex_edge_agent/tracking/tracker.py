from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from scipy.optimize import linear_sum_assignment  # type: ignore[import-untyped]

from veotrex_edge_agent.tracking.kalman import (
    BoxKalmanFilter,
    KalmanStateError,
    xyah_to_xyxy,
    xyxy_to_xyah,
)


class TrackState(StrEnum):
    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    LOST = "LOST"
    REMOVED = "REMOVED"


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    low_score_threshold: float = 0.05
    high_score_threshold: float = 0.30
    new_track_threshold: float = 0.30
    first_max_association_cost: float = 0.80
    second_max_association_cost: float = 0.50
    tentative_max_association_cost: float = 0.70
    max_lost_seconds: float = 2.0
    max_timestamp_gap_seconds: float = 2.5
    confirmation_observations: int = 2
    max_detections_per_frame: int = 300
    max_active_tracks: int = 128
    max_lost_tracks: int = 128
    max_history_samples: int = 32
    max_streams: int = 16

    def __post_init__(self) -> None:
        if not 0 <= self.low_score_threshold <= self.high_score_threshold <= 1:
            raise ValueError("invalid_score_thresholds")
        if not self.new_track_threshold >= self.high_score_threshold:
            raise ValueError("invalid_new_track_threshold")
        if min(self.max_lost_seconds, self.max_timestamp_gap_seconds) <= 0:
            raise ValueError("invalid_time_bounds")
        if (
            min(
                self.confirmation_observations,
                self.max_detections_per_frame,
                self.max_active_tracks,
                self.max_lost_tracks,
                self.max_history_samples,
                self.max_streams,
            )
            < 1
        ):
            raise ValueError("invalid_capacity")


@dataclass(frozen=True, slots=True)
class PersonDetection:
    bbox_xyxy_source: tuple[float, float, float, float]
    score: float


@dataclass(frozen=True, slots=True)
class TrackView:
    stream_instance_id: str
    track_id: int
    state: TrackState
    bbox_xyxy_source: tuple[float, float, float, float]
    latest_detection_score: float
    first_seen_timestamp: float
    last_seen_timestamp: float
    age_seconds: float
    observation_count: int
    consecutive_misses: int
    was_low_score_recovery: bool


@dataclass(frozen=True, slots=True)
class TrackingFrameResult:
    stream_instance_id: str
    frame_sequence: int
    timestamp: float
    confirmed_tracks: tuple[TrackView, ...]
    tentative_tracks: tuple[TrackView, ...]
    lost_track_ids: tuple[int, ...]
    recovered_track_ids: tuple[int, ...]
    removed_track_ids: tuple[int, ...]
    discontinuity: bool
    rejected_detections: int
    tracking_update_ms: float


@dataclass(slots=True)
class TrackingMetrics:
    tracking_frames_total: int = 0
    tracking_tracks_created_total: int = 0
    tracking_tracks_confirmed_total: int = 0
    tracking_tracks_lost_total: int = 0
    tracking_tracks_recovered_total: int = 0
    tracking_tracks_removed_total: int = 0
    tracking_low_score_recoveries_total: int = 0
    tracking_discontinuities_total: int = 0
    tracking_capacity_drops_total: int = 0
    tracking_errors_total: int = 0
    tracking_update_ms: float = 0.0


@dataclass(slots=True)
class _Track:
    track_id: int
    state: TrackState
    mean: np.ndarray
    covariance: np.ndarray
    first_seen: float
    last_seen: float
    score: float
    observations: int = 1
    misses: int = 0
    low_recovery: bool = False
    history: deque[tuple[float, tuple[float, float, float, float]]] = field(default_factory=deque)

    @property
    def box(self) -> tuple[float, float, float, float]:
        return xyah_to_xyxy(self.mean)


@dataclass(slots=True)
class _Stream:
    width: int
    height: int
    sequence: int | None = None
    timestamp: float | None = None
    next_track_id: int = 1
    tracks: list[_Track] = field(default_factory=list)


def iou_matrix(tracks: list[_Track], detections: list[PersonDetection]) -> np.ndarray:
    result = np.zeros((len(tracks), len(detections)), dtype=np.float64)
    for row, track in enumerate(tracks):
        ax1, ay1, ax2, ay2 = track.box
        for column, detection in enumerate(detections):
            bx1, by1, bx2, by2 = detection.bbox_xyxy_source
            intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
                0.0, min(ay2, by2) - max(ay1, by1)
            )
            union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection
            result[row, column] = intersection / union if union > 0 else 0.0
    return result


def _associate(
    tracks: list[_Track], detections: list[PersonDetection], maximum_cost: float
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    if not tracks or not detections:
        return [], list(range(len(tracks))), list(range(len(detections)))
    cost = 1.0 - iou_matrix(tracks, detections)
    # Stable index-scale perturbation only resolves mathematically equal assignments.
    tie = np.arange(cost.size, dtype=np.float64).reshape(cost.shape) * np.finfo(float).eps
    rows, columns = linear_sum_assignment(cost + tie)
    matches = [
        (int(r), int(c)) for r, c in zip(rows, columns, strict=True) if cost[r, c] <= maximum_cost
    ]
    matched_rows, matched_columns = {x[0] for x in matches}, {x[1] for x in matches}
    return (
        matches,
        [i for i in range(len(tracks)) if i not in matched_rows],
        [i for i in range(len(detections)) if i not in matched_columns],
    )


class PersonTracker:
    """Bounded, deterministic, per-stream ByteTrack-style PERSON tracker."""

    def __init__(self, config: TrackingConfig | None = None) -> None:
        self.config = config or TrackingConfig()
        self.metrics = TrackingMetrics()
        self._streams: dict[str, _Stream] = {}
        self._kalman = BoxKalmanFilter()

    def update(
        self,
        stream_instance_id: str,
        frame_sequence: int,
        timestamp: float,
        detections: list[PersonDetection],
        *,
        source_width: int,
        source_height: int,
        source_discontinuity: bool = False,
    ) -> TrackingFrameResult:
        """Advance one stream by one frame.

        ``source_discontinuity`` is the source saying the feed was interrupted before this
        frame (a reconnect). It clears the stream exactly as an over-long timestamp gap does -
        same metrics, same continuing track ids - because motion across an interruption is not
        motion, however short the interruption was. It is ignored on a stream's first frame,
        where there is no earlier state to separate from.
        """
        started = time.perf_counter_ns()
        if not stream_instance_id or frame_sequence < 0 or not math.isfinite(timestamp):
            self.metrics.tracking_errors_total += 1
            raise ValueError("invalid_frame_metadata")
        if source_width < 1 or source_height < 1:
            raise ValueError("invalid_source_dimensions")
        stream = self._streams.get(stream_instance_id)
        discontinuity = False
        if stream is None:
            if len(self._streams) >= self.config.max_streams:
                self.metrics.tracking_capacity_drops_total += 1
                raise ValueError("stream_capacity_exceeded")
            stream = _Stream(source_width, source_height)
            self._streams[stream_instance_id] = stream
        elif (stream.width, stream.height) != (source_width, source_height):
            self._remove_all(stream)
            stream = _Stream(source_width, source_height)
            self._streams[stream_instance_id] = stream
            discontinuity = True
        if stream.sequence is not None and frame_sequence <= stream.sequence:
            self.metrics.tracking_errors_total += 1
            raise ValueError("non_monotonic_frame_sequence")
        if stream.timestamp is not None and timestamp < stream.timestamp:
            self.metrics.tracking_errors_total += 1
            raise ValueError("non_monotonic_timestamp")
        if stream.timestamp is not None and (
            source_discontinuity
            or timestamp - stream.timestamp > self.config.max_timestamp_gap_seconds
        ):
            self._remove_all(stream)
            discontinuity = True
        if discontinuity:
            self.metrics.tracking_discontinuities_total += 1
        elapsed = 0.0 if stream.timestamp is None else timestamp - stream.timestamp
        stream.sequence, stream.timestamp = frame_sequence, timestamp
        valid = [
            item for item in detections if self._valid_detection(item, source_width, source_height)
        ]
        rejected = len(detections) - len(valid)
        if len(valid) > self.config.max_detections_per_frame:
            valid = sorted(valid, key=lambda x: (-x.score, x.bbox_xyxy_source))[
                : self.config.max_detections_per_frame
            ]
            rejected = len(detections) - len(valid)
            self.metrics.tracking_capacity_drops_total += rejected
        high = [x for x in valid if x.score >= self.config.high_score_threshold]
        low = [
            x
            for x in valid
            if self.config.low_score_threshold <= x.score < self.config.high_score_threshold
        ]
        recovered: list[int] = []
        lost: list[int] = []
        removed: list[int] = []
        for track in stream.tracks:
            if (
                track.state is TrackState.LOST
                and timestamp - track.last_seen > self.config.max_lost_seconds
            ):
                track.state = TrackState.REMOVED
                removed.append(track.track_id)
                self.metrics.tracking_tracks_removed_total += 1
        stream.tracks = [x for x in stream.tracks if x.state is not TrackState.REMOVED]
        pool = [x for x in stream.tracks if x.state in (TrackState.CONFIRMED, TrackState.LOST)]
        tentative = [x for x in stream.tracks if x.state is TrackState.TENTATIVE]
        for track in pool + tentative:
            try:
                track.mean, track.covariance = self._kalman.predict(
                    track.mean, track.covariance, elapsed
                )
            except KalmanStateError:
                track.state = TrackState.REMOVED
                self.metrics.tracking_errors_total += 1
        pool = [x for x in pool if x.state is not TrackState.REMOVED]
        tentative = [x for x in tentative if x.state is not TrackState.REMOVED]
        matches, unmatched_pool, unmatched_high = _associate(
            pool, high, self.config.first_max_association_cost
        )
        for ti, di in matches:
            was_lost = pool[ti].state is TrackState.LOST
            self._observe(pool[ti], high[di], timestamp, low=False)
            if was_lost:
                recovered.append(pool[ti].track_id)
                self.metrics.tracking_tracks_recovered_total += 1
        second_tracks = [pool[i] for i in unmatched_pool if pool[i].state is TrackState.CONFIRMED]
        matches2, unmatched_second, _ = _associate(
            second_tracks, low, self.config.second_max_association_cost
        )
        for ti, di in matches2:
            self._observe(second_tracks[ti], low[di], timestamp, low=True)
            self.metrics.tracking_low_score_recoveries_total += 1
        matched_second_ids = {second_tracks[i].track_id for i, _ in matches2}
        for index in unmatched_pool:
            track = pool[index]
            if track.track_id in matched_second_ids or track.state is TrackState.LOST:
                continue
            track.state = TrackState.LOST
            track.misses += 1
            lost.append(track.track_id)
            self.metrics.tracking_tracks_lost_total += 1
        remaining_high = [high[i] for i in unmatched_high]
        matches3, unmatched_tentative, unmatched_remaining = _associate(
            tentative, remaining_high, self.config.tentative_max_association_cost
        )
        for ti, di in matches3:
            self._observe(tentative[ti], remaining_high[di], timestamp, low=False)
            if tentative[ti].observations >= self.config.confirmation_observations:
                tentative[ti].state = TrackState.CONFIRMED
                self.metrics.tracking_tracks_confirmed_total += 1
        for index in unmatched_tentative:
            tentative[index].state = TrackState.REMOVED
            removed.append(tentative[index].track_id)
            self.metrics.tracking_tracks_removed_total += 1
        for index in unmatched_remaining:
            detection = remaining_high[index]
            if detection.score >= self.config.new_track_threshold:
                self._create(stream, detection, timestamp)
        self._suppress_duplicates(stream, removed)
        self._bound_tracks(stream, removed)
        self.metrics.tracking_frames_total += 1
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        self.metrics.tracking_update_ms = elapsed_ms
        views = [self._view(stream_instance_id, x, timestamp) for x in stream.tracks]
        return TrackingFrameResult(
            stream_instance_id,
            frame_sequence,
            timestamp,
            tuple(x for x in views if x.state is TrackState.CONFIRMED),
            tuple(x for x in views if x.state is TrackState.TENTATIVE),
            tuple(sorted(lost)),
            tuple(sorted(recovered)),
            tuple(sorted(set(removed))),
            discontinuity,
            rejected,
            elapsed_ms,
        )

    def reset(self, stream_instance_id: str) -> None:
        stream = self._streams.pop(stream_instance_id, None)
        if stream is not None:
            self._remove_all(stream)

    def status(self, stream_instance_id: str) -> dict[str, int]:
        stream = self._streams.get(stream_instance_id)
        tracks = [] if stream is None else stream.tracks
        return {
            "active_tracks": sum(
                track.state in (TrackState.TENTATIVE, TrackState.CONFIRMED) for track in tracks
            ),
            "lost_tracks": sum(track.state is TrackState.LOST for track in tracks),
            "retained_history_samples": sum(len(track.history) for track in tracks),
        }

    def _create(self, stream: _Stream, detection: PersonDetection, timestamp: float) -> None:
        active = sum(x.state in (TrackState.TENTATIVE, TrackState.CONFIRMED) for x in stream.tracks)
        if active >= self.config.max_active_tracks:
            self.metrics.tracking_capacity_drops_total += 1
            return
        mean, covariance = self._kalman.initiate(xyxy_to_xyah(detection.bbox_xyxy_source))
        state = (
            TrackState.CONFIRMED
            if self.config.confirmation_observations == 1
            else TrackState.TENTATIVE
        )
        track = _Track(
            stream.next_track_id, state, mean, covariance, timestamp, timestamp, detection.score
        )
        track.history = deque([(timestamp, track.box)], maxlen=self.config.max_history_samples)
        stream.next_track_id += 1
        stream.tracks.append(track)
        self.metrics.tracking_tracks_created_total += 1
        if state is TrackState.CONFIRMED:
            self.metrics.tracking_tracks_confirmed_total += 1

    def _observe(
        self, track: _Track, detection: PersonDetection, timestamp: float, *, low: bool
    ) -> None:
        track.mean, track.covariance = self._kalman.update(
            track.mean, track.covariance, xyxy_to_xyah(detection.bbox_xyxy_source)
        )
        track.last_seen, track.score, track.misses = timestamp, detection.score, 0
        track.observations += 1
        track.low_recovery = low
        # A match re-confirms an established (CONFIRMED or LOST) track. A TENTATIVE track is
        # promoted only by its caller, once it has ``confirmation_observations``; promoting it
        # here confirmed every tentative track on its second observation whatever the config
        # said (V1-03B).
        if track.state is not TrackState.TENTATIVE:
            track.state = TrackState.CONFIRMED
        track.history.append((timestamp, track.box))

    def _bound_tracks(self, stream: _Stream, removed: list[int]) -> None:
        lost = sorted(
            (x for x in stream.tracks if x.state is TrackState.LOST),
            key=lambda x: (x.last_seen, x.track_id),
        )
        for track in lost[: max(0, len(lost) - self.config.max_lost_tracks)]:
            track.state = TrackState.REMOVED
            removed.append(track.track_id)
            self.metrics.tracking_capacity_drops_total += 1
            self.metrics.tracking_tracks_removed_total += 1
        stream.tracks = [x for x in stream.tracks if x.state is not TrackState.REMOVED]

    def _suppress_duplicates(self, stream: _Stream, removed: list[int]) -> None:
        active = [x for x in stream.tracks if x.state is TrackState.CONFIRMED]
        duplicate_ids: set[int] = set()
        for left_index, left in enumerate(active):
            for right in active[left_index + 1 :]:
                if iou_matrix([left], [PersonDetection(right.box, right.score)])[0, 0] < 0.85:
                    continue
                loser = min(
                    (left, right), key=lambda x: (x.observations, x.first_seen, -x.track_id)
                )
                duplicate_ids.add(loser.track_id)
        for track in stream.tracks:
            if track.track_id in duplicate_ids:
                track.state = TrackState.REMOVED
                removed.append(track.track_id)
                self.metrics.tracking_tracks_removed_total += 1
        stream.tracks = [x for x in stream.tracks if x.state is not TrackState.REMOVED]

    def _remove_all(self, stream: _Stream) -> None:
        self.metrics.tracking_tracks_removed_total += len(stream.tracks)
        stream.tracks.clear()

    @staticmethod
    def _valid_detection(item: PersonDetection, width: int, height: int) -> bool:
        box = item.bbox_xyxy_source
        return (
            math.isfinite(item.score)
            and 0 <= item.score <= 1
            and len(box) == 4
            and all(math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= width
            and 0 <= box[1] < box[3] <= height
        )

    @staticmethod
    def _view(stream_id: str, track: _Track, timestamp: float) -> TrackView:
        return TrackView(
            stream_id,
            track.track_id,
            track.state,
            track.box,
            track.score,
            track.first_seen,
            track.last_seen,
            timestamp - track.first_seen,
            track.observations,
            track.misses,
            track.low_recovery,
        )
