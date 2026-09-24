"""Recorded-video person tracking pipeline (V1-02B1A).

Streams one local video through detection and tracking and emits lifecycle facts:

    RecordedVideoSource -> PersonDetector -> PersonTracker -> TrackObservation / TrackSummary

The tracker is the existing ``veotrex_edge_agent.tracking.PersonTracker`` (ADR 0012), used
unmodified. Everything this module adds is the part that was missing: a streaming source, a
detector boundary, lifecycle bookkeeping across frames, and bounded metrics.

Three properties are deliberate and load-bearing:

*No identity anywhere.* Tracking is driven entirely by motion and geometry. A person who is
not enrolled, who is a child, or whose face is never visible is tracked exactly as well as
anyone else, because nothing here knows what a face is. Recognition is a later, optional
annotation onto a track that already exists - never an input to forming one.

*Bounded memory.* One decoded frame is live at a time, per-track history is capped by the
tracker, latency samples go into fixed-capacity reservoirs, and a completed track is emitted
and dropped rather than accumulated. Processing a two-hour recording costs what processing two
minutes costs.

*Tracker state is per run.* A fresh ``PersonTracker`` is built per job and the stream id is
derived from the run, so no track id, Kalman state or capacity count can survive from one
video into the next.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veotrex_edge_agent.qualification.metrics import BoundedSamples
from veotrex_edge_agent.recorded.detector import BoundingBoxValidator, PersonDetector
from veotrex_edge_agent.recorded.model import (
    TrackEndReason,
    TrackLifecycle,
    TrackObservation,
    TrackSummary,
)
from veotrex_edge_agent.recorded.regions import IgnoreRegionSet
from veotrex_edge_agent.recorded.source import RecordedVideoSource, VideoMetadata
from veotrex_edge_agent.tracking import PersonDetection, PersonTracker, TrackingConfig, TrackView

# Latency reservoirs. Fixed capacity: percentiles stay meaningful without the sample set
# growing with the length of the video.
LATENCY_SAMPLE_CAPACITY = 4096

# (frame image, [(track_id, bbox_xyxy)], timestamp_ms). The image is the live frame and
# must not be retained by the hook past the call.
FrameHook = Callable[[Any, Sequence[tuple[int, tuple[float, ...]]], float], None]


@dataclass(slots=True)
class PipelineMetrics:
    """Counters and latency reservoirs for one run. Bounded by construction."""

    video_frames_processed_total: int = 0
    video_frames_dropped_total: int = 0
    person_detections_total: int = 0
    detections_rejected_total: int = 0
    # Detections dropped by an operator-configured ignore region. Counted separately from
    # rejections: a rejection is a malformed box, this is a well-formed box in a place the
    # operator has said is furniture.
    detections_ignored_total: int = 0
    tracks_created_total: int = 0
    tracks_completed_total: int = 0
    active_tracks: int = 0
    peak_active_tracks: int = 0
    detector_latency_ms: BoundedSamples = field(
        default_factory=lambda: BoundedSamples(LATENCY_SAMPLE_CAPACITY)
    )
    tracker_latency_ms: BoundedSamples = field(
        default_factory=lambda: BoundedSamples(LATENCY_SAMPLE_CAPACITY)
    )
    pipeline_latency_ms: BoundedSamples = field(
        default_factory=lambda: BoundedSamples(LATENCY_SAMPLE_CAPACITY)
    )
    processing_seconds: float = 0.0

    @property
    def processing_fps(self) -> float:
        if self.processing_seconds <= 0:
            return 0.0
        return self.video_frames_processed_total / self.processing_seconds

    @staticmethod
    def _summary(samples: BoundedSamples) -> dict[str, float | None]:
        """Percentiles come from the reservoir's retained window, which is the whole run for
        anything shorter than its capacity and the most recent samples beyond that."""
        return {
            "count": float(samples.count),
            "retained": float(samples.retained_count),
            "p50": samples.percentile(50),
            "p95": samples.percentile(95),
            "max": samples.maximum,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "video_frames_processed_total": self.video_frames_processed_total,
            "video_frames_dropped_total": self.video_frames_dropped_total,
            "person_detections_total": self.person_detections_total,
            "detections_rejected_total": self.detections_rejected_total,
            "detections_ignored_total": self.detections_ignored_total,
            "tracks_created_total": self.tracks_created_total,
            "tracks_completed_total": self.tracks_completed_total,
            "active_tracks": self.active_tracks,
            "peak_active_tracks": self.peak_active_tracks,
            "processing_seconds": round(self.processing_seconds, 4),
            "processing_fps": round(self.processing_fps, 3),
            "detector_latency_ms": self._summary(self.detector_latency_ms),
            "tracker_latency_ms": self._summary(self.tracker_latency_ms),
            "pipeline_latency_ms": self._summary(self.pipeline_latency_ms),
        }


@dataclass(frozen=True, slots=True)
class PipelineRecord:
    """One emitted record: either a per-frame observation or a completed track."""

    kind: str
    observation: TrackObservation | None = None
    summary: TrackSummary | None = None


@dataclass(slots=True)
class _LiveTrack:
    """The pipeline's own bookkeeping for a track the tracker is following.

    The tracker knows geometry; this knows the narrative - when the track was first reported,
    how many times it has been seen, and whether TRACK_STARTED has been emitted yet.
    """

    first_seen_ms: float
    last_seen_ms: float
    observation_count: int
    maximum_confidence: float
    started_emitted: bool = False


class RecordedTrackingPipeline:
    """Runs one video to completion, yielding records as they are decided.

    Results are produced lazily so an operator sees output while a long recording is still
    being processed, and so nothing has to be held in memory to be written.
    """

    def __init__(
        self,
        detector: PersonDetector,
        *,
        tracking_config: TrackingConfig | None = None,
        sample_every: int = 1,
        max_frames: int | None = None,
        ignore_regions: IgnoreRegionSet | None = None,
    ) -> None:
        self._detector = detector
        self._tracking_config = tracking_config or TrackingConfig()
        # Empty unless a camera has been configured, and an empty set is not consulted at all,
        # so every existing caller keeps its exact behaviour.
        self.ignore_regions = ignore_regions or IgnoreRegionSet()
        self._sample_every = max(1, sample_every)
        self._max_frames = max_frames
        self.metrics = PipelineMetrics()
        self._live: dict[int, _LiveTrack] = {}
        self._video_metadata: VideoMetadata | None = None

    @property
    def video_metadata(self) -> VideoMetadata | None:
        return self._video_metadata

    def _observe(
        self,
        view: TrackView,
        frame_index: int,
        timestamp_ms: float,
        lifecycle: TrackLifecycle,
    ) -> TrackObservation:
        return TrackObservation(
            track_id=view.track_id,
            frame_index=frame_index,
            timestamp_ms=timestamp_ms,
            bbox_xyxy=tuple(round(float(value), 2) for value in view.bbox_xyxy_source),  # type: ignore[arg-type]
            detection_confidence=round(float(view.latest_detection_score), 4),
            track_state=str(view.state),
            lifecycle=lifecycle,
        )

    def _end(self, track_id: int, reason: TrackEndReason) -> PipelineRecord | None:
        live = self._live.pop(track_id, None)
        if live is None or not live.started_emitted:
            # A track that never reached CONFIRMED was never reported as started, so there is
            # nothing to close: emitting an end for it would invent a track from nothing.
            return None
        self.metrics.tracks_completed_total += 1
        return PipelineRecord(
            kind="track_summary",
            summary=TrackSummary(
                track_id=track_id,
                first_seen_ms=round(live.first_seen_ms, 3),
                last_seen_ms=round(live.last_seen_ms, 3),
                observation_count=live.observation_count,
                maximum_confidence=round(live.maximum_confidence, 4),
                end_reason=reason,
            ),
        )

    def run(
        self,
        video: Path,
        *,
        run_id: str | None = None,
        frame_hook: FrameHook | None = None,
    ) -> Iterator[PipelineRecord]:
        """Process one video, yielding observations and completed-track summaries.

        The tracker and its stream id are created here rather than held on the instance, so
        two runs cannot share state even if the same pipeline object is reused.

        ``frame_hook`` is called once per processed frame with that frame and the confirmed
        boxes on it. It exists so an annotated evaluation video can be produced from this
        single decode pass; without it nothing ever sees a frame after detection.
        """
        with RecordedVideoSource(video, sample_every=self._sample_every) as source:
            self._video_metadata = source.metadata
            yield from self.process(source.frames(), run_id=run_id, frame_hook=frame_hook)
            self.metrics.video_frames_dropped_total = source.frames_failed

    def process(
        self,
        frames: Iterable[Any],
        *,
        run_id: str | None = None,
        frame_hook: FrameHook | None = None,
    ) -> Iterator[PipelineRecord]:
        """Track over an arbitrary sequence of frames.

        Split out from ``run`` so the whole of detection, tracking, lifecycle and metrics can
        be exercised against constructed frames - no video file, no codec, no OpenCV. That is
        what lets CI cover this logic on any machine.
        """
        tracker = PersonTracker(self._tracking_config)
        stream_id = run_id or f"recorded-{int(time.time() * 1000)}"
        self._live.clear()
        started_wall = time.perf_counter()
        try:
            for frame in frames:
                if self._max_frames is not None and frame.frame_index >= self._max_frames:
                    break
                yield from self._process_frame(tracker, stream_id, frame, frame_hook)
            # The stream ended while some tracks were still live. That is not a disappearance
            # and must not be recorded as one.
            for track_id in sorted(self._live):
                record = self._end(track_id, TrackEndReason.STREAM_ENDED)
                if record is not None:
                    yield record
            self._live.clear()
        finally:
            self.metrics.processing_seconds = time.perf_counter() - started_wall
            self.metrics.active_tracks = 0
            # Whatever happened, no tracker state outlives the job.
            tracker.reset(stream_id)

    def _process_frame(
        self,
        tracker: PersonTracker,
        stream_id: str,
        frame: Any,
        frame_hook: FrameHook | None = None,
    ) -> Iterator[PipelineRecord]:
        frame_started = time.perf_counter_ns()
        detector_started = time.perf_counter_ns()
        detections = self._detector.detect(
            frame.image, frame_index=frame.frame_index, timestamp_ms=frame.timestamp_ms
        )
        detector_ms = (time.perf_counter_ns() - detector_started) / 1e6

        validator = BoundingBoxValidator(frame.width, frame.height)
        accepted, rejected = validator.validate(detections)
        self.metrics.detections_rejected_total += rejected
        if self.ignore_regions:
            # Before the tracker, deliberately. A detection that reached PersonTracker would
            # already have created or fed a track, so suppressing it later would leave a track
            # id that appeared and then went quiet - which is exactly what a person leaving
            # looks like. Dropping it here means the phantom never existed.
            kept = []
            for item in accepted:
                if self.ignore_regions.matching(
                    item.bbox_xyxy, width=frame.width, height=frame.height
                ):
                    self.metrics.detections_ignored_total += 1
                else:
                    kept.append(item)
            accepted = kept
        self.metrics.person_detections_total += len(accepted)

        tracker_started = time.perf_counter_ns()
        result = tracker.update(
            stream_id,
            frame.frame_index,
            # The tracker's time base is seconds; the emitted contract is milliseconds on the
            # media timeline. Converting here keeps both honest.
            frame.timestamp_ms / 1000.0,
            [PersonDetection(item.bbox_xyxy, item.confidence) for item in accepted],
            source_width=frame.width,
            source_height=frame.height,
        )
        tracker_ms = (time.perf_counter_ns() - tracker_started) / 1e6

        yield from self._apply(result, frame)

        if frame_hook is not None:
            frame_hook(
                frame.image,
                [
                    (int(view.track_id), tuple(float(v) for v in view.bbox_xyxy_source))
                    for view in result.confirmed_tracks
                ],
                frame.timestamp_ms,
            )

        self.metrics.video_frames_processed_total += 1
        self.metrics.detector_latency_ms.add(detector_ms)
        self.metrics.tracker_latency_ms.add(tracker_ms)
        self.metrics.pipeline_latency_ms.add((time.perf_counter_ns() - frame_started) / 1e6)
        self.metrics.active_tracks = len(self._live)
        self.metrics.peak_active_tracks = max(
            self.metrics.peak_active_tracks, self.metrics.active_tracks
        )

    def _apply(self, result: Any, frame: Any) -> Iterator[PipelineRecord]:
        """Turn one tracker frame result into lifecycle records.

        Only CONFIRMED tracks are reported. A TENTATIVE track is the tracker's hypothesis and
        may evaporate; publishing one would mean emitting a track id that later turns out
        never to have been a person.
        """
        for view in result.confirmed_tracks:
            live = self._live.get(view.track_id)
            if live is None:
                live = _LiveTrack(
                    first_seen_ms=frame.timestamp_ms,
                    last_seen_ms=frame.timestamp_ms,
                    observation_count=0,
                    maximum_confidence=0.0,
                )
                self._live[view.track_id] = live
                self.metrics.tracks_created_total += 1
            lifecycle = (
                TrackLifecycle.TRACK_ACTIVE
                if live.started_emitted
                else TrackLifecycle.TRACK_STARTED
            )
            live.started_emitted = True
            live.last_seen_ms = frame.timestamp_ms
            live.observation_count += 1
            live.maximum_confidence = max(
                live.maximum_confidence, float(view.latest_detection_score)
            )
            yield PipelineRecord(
                kind="observation",
                observation=self._observe(view, frame.frame_index, frame.timestamp_ms, lifecycle),
            )
        for track_id in result.removed_track_ids:
            record = self._end(int(track_id), TrackEndReason.ABSENT)
            if record is not None:
                yield record
