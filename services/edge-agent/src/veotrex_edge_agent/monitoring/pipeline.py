"""The monitoring loop: frame source -> detector -> tracker -> occupancy -> published state.

This is the only place the pieces are joined. It runs the production detector and the
production tracker; a recorded clip and a camera frame take the identical path once decoded,
which is the whole point of the recorded source existing.

Every published metric is measured. When a value has not been measured yet it is published as
``None`` so the UI can render absence instead of inventing a number.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import structlog
from numpy.typing import NDArray

from veotrex_edge_agent.frame_source.recorded_video import RecordedVideoSource
from veotrex_edge_agent.frame_source.source import SourceFrame, SourceHealth, SourceKind
from veotrex_edge_agent.image_pipeline import PixelFormat
from veotrex_edge_agent.monitoring.events import SafetyEvent, SafetyEventKind, SessionEventLog
from veotrex_edge_agent.monitoring.occupancy import (
    CoverageState,
    DemoStaffingPolicy,
    OccupancyReading,
    coverage_for,
    read_occupancy,
)
from veotrex_edge_agent.monitoring.overlay import annotate
from veotrex_edge_agent.tracking import (
    PersonDetection,
    PersonTracker,
    TrackingConfig,
    TrackView,
)

FPS_WINDOW = 30


class Detector(Protocol):
    """The detector contract the loop needs. Satisfied by ReferenceImageDetector."""

    def infer_decoded(
        self,
        image: NDArray[np.uint8],
        *,
        pixel_format: PixelFormat,
        frame_id: str,
        profile: Any = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PipelineSnapshot:
    source_kind: SourceKind
    source_health: SourceHealth
    area_label: str
    camera_label: str
    occupancy: OccupancyReading
    active_track_count: int | None
    # None until measured. Never a placeholder, never a guess.
    measured_fps: float | None
    inference_latency_ms: float | None
    frames_processed: int
    loops_completed: int
    decode_failures: int
    seconds_since_last_frame: float | None
    media_timestamp_seconds: float | None
    source_error_category: str | None
    # Session aggregates. Counted from real tracker output over the life of the process.
    peak_occupancy: int
    tracks_observed: int
    longest_track_seconds: float | None
    session_seconds: float
    events: tuple[SafetyEvent, ...]


class MonitoringPipeline:
    """Owns one source, one tracker and the published state for one area."""

    def __init__(
        self,
        source: RecordedVideoSource,
        detector: Detector,
        *,
        area_label: str,
        camera_label: str,
        policy: DemoStaffingPolicy | None = None,
        tracking_config: TrackingConfig | None = None,
        stale_after_seconds: float = 3.0,
        jpeg_quality: int = 85,
        clock: Callable[[], float] = time.monotonic,
        detection_profile: Any = None,
    ) -> None:
        self._source = source
        self._detector = detector
        self._area_label = area_label
        self._camera_label = camera_label
        self._policy = policy
        self._tracker = PersonTracker(tracking_config)
        self._stale_after = stale_after_seconds
        self._jpeg_quality = jpeg_quality
        self._clock = clock
        self._profile = detection_profile
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._events = SessionEventLog(area_label)
        self._logger = structlog.get_logger()

        self._latest_jpeg: bytes | None = None
        self._latest_tracks: tuple[TrackView, ...] = ()
        self._frames_processed = 0
        self._last_frame_at: float | None = None
        self._frame_intervals: deque[float] = deque(maxlen=FPS_WINDOW)
        self._latency_samples: deque[float] = deque(maxlen=FPS_WINDOW)
        self._media_timestamp: float | None = None
        self._last_reading: OccupancyReading | None = None
        self._failure_category: str | None = None
        self._peak_occupancy = 0
        self._observed_track_ids: set[int] = set()
        self._confirmed_track_ids: set[int] = set()
        self._longest_track_seconds: float | None = None
        self._started_at = clock()

    # ---- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._source.start()
        thread = threading.Thread(target=self._run, name="veotrex-monitoring", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._source.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
        self._thread = None

    def restart_source(self) -> None:
        """Operator control: play the clip from the beginning."""
        self._source.restart()

    # ---- published state ----------------------------------------------------------

    def latest_frame_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def snapshot(self) -> PipelineSnapshot:
        """Coverage is recomputed at read time.

        If the worker thread dies or wedges, the dashboard must degrade to IMPAIRED on its
        own rather than keep serving the last happy state forever.
        """
        with self._lock:
            now = self._clock()
            status = self._source.status()
            since = None if self._last_frame_at is None else now - self._last_frame_at
            # Source health alone, deliberately: a worker thread that has died while the
            # source still claims RUNNING shows up as staleness a moment later, which is
            # the same IMPAIRED verdict by a route that also catches a merely wedged one.
            running = status.health is SourceHealth.RUNNING
            coverage = coverage_for(
                source_running=running,
                seconds_since_last_frame=since,
                stale_after_seconds=self._stale_after,
            )
            if coverage is CoverageState.ACTIVE and self._last_reading is not None:
                occupancy = self._last_reading
                active = len(self._latest_tracks)
            else:
                occupancy = read_occupancy(
                    None, coverage=coverage, observed_at_monotonic=now, policy=self._policy
                )
                active = None
            return PipelineSnapshot(
                source_kind=self._source.kind,
                source_health=status.health,
                area_label=self._area_label,
                camera_label=self._camera_label,
                occupancy=occupancy,
                active_track_count=active,
                measured_fps=self._measured_fps(),
                inference_latency_ms=self._measured_latency(),
                frames_processed=self._frames_processed,
                loops_completed=status.loops_completed,
                decode_failures=status.decode_failures,
                seconds_since_last_frame=None if since is None else round(since, 2),
                media_timestamp_seconds=self._media_timestamp,
                source_error_category=self._failure_category or status.last_error_category,
                peak_occupancy=self._peak_occupancy,
                tracks_observed=len(self._observed_track_ids),
                longest_track_seconds=self._longest_track_seconds,
                session_seconds=round(now - self._started_at, 1),
                events=self._events.events(),
            )

    def _measured_fps(self) -> float | None:
        if len(self._frame_intervals) < 2:
            return None
        mean = sum(self._frame_intervals) / len(self._frame_intervals)
        return None if mean <= 0 else round(1.0 / mean, 1)

    def _measured_latency(self) -> float | None:
        if not self._latency_samples:
            return None
        return round(sum(self._latency_samples) / len(self._latency_samples), 1)

    # ---- worker -------------------------------------------------------------------

    def _run(self) -> None:
        try:
            for frame in self._source.frames():
                if self._stop.is_set():
                    break
                self._process(frame)
        except Exception as exc:
            category = getattr(exc, "category", type(exc).__name__)
            with self._lock:
                self._failure_category = str(category)
            self._logger.error("monitoring_pipeline_failed", category=str(category))

    def _process(self, frame: SourceFrame) -> None:
        inference = self._detector.infer_decoded(
            frame.rgb,
            pixel_format=PixelFormat.RGB8,
            frame_id=f"{frame.stream_instance_id}-{frame.sequence}",
            **({"profile": self._profile} if self._profile is not None else {}),
        )
        detections = [
            value for item in inference.get("detections", []) if (value := _detection(item))
        ]
        tracking = self._tracker.update(
            frame.stream_instance_id,
            frame.sequence,
            frame.monotonic_timestamp_seconds,
            detections,
            source_width=frame.width,
            source_height=frame.height,
        )
        annotated = annotate(frame.rgb, tracking.confirmed_tracks, jpeg_quality=self._jpeg_quality)
        now = self._clock()
        timing = inference.get("image_timing") or {}
        latency = timing.get("decoded_frame_to_detection_ms")
        with self._lock:
            if self._last_frame_at is not None:
                interval = now - self._last_frame_at
                if interval > 0:
                    self._frame_intervals.append(interval)
            self._last_frame_at = now
            self._latest_jpeg = annotated
            self._latest_tracks = tracking.confirmed_tracks
            self._media_timestamp = frame.media_timestamp_seconds
            self._frames_processed += 1
            if isinstance(latency, int | float):
                self._latency_samples.append(float(latency))
            reading = read_occupancy(
                tracking,
                coverage=CoverageState.ACTIVE,
                observed_at_monotonic=now,
                policy=self._policy,
            )
            self._last_reading = reading
            self._events.observe(reading)
            self._record_track_lifecycle(tracking, reading)

    def _record_track_lifecycle(self, tracking: Any, reading: OccupancyReading) -> None:
        """Real track appearances and disappearances, plus session aggregates.

        Called with the lock held. Every number here comes from the tracker: a track that
        started is one the tracker confirmed, not a heuristic over detections.
        """
        confirmed = {track.track_id for track in tracking.confirmed_tracks}
        for track_id in sorted(confirmed - self._confirmed_track_ids):
            self._observed_track_ids.add(track_id)
            self._events.record(SafetyEventKind.TRACK_STARTED, reading)
        for _ in sorted(self._confirmed_track_ids - confirmed):
            self._events.record(SafetyEventKind.TRACK_ENDED, reading)
        previous_count = len(self._confirmed_track_ids)
        self._confirmed_track_ids = confirmed
        for track in tracking.confirmed_tracks:
            age = float(track.age_seconds)
            if self._longest_track_seconds is None or age > self._longest_track_seconds:
                self._longest_track_seconds = round(age, 1)
        people = len(confirmed)
        if people != previous_count:
            self._events.record(SafetyEventKind.OCCUPANCY_CHANGED, reading)
        if people > self._peak_occupancy:
            self._peak_occupancy = people
            self._events.record(SafetyEventKind.PEAK_OCCUPANCY, reading)


def _detection(item: object) -> PersonDetection | None:
    """Same shape the offline replay harness consumes, so both agree on the contract."""
    if not isinstance(item, dict):
        return None
    box = item.get("bbox_xyxy_source")
    score = item.get("score")
    if not isinstance(box, dict) or not isinstance(score, int | float):
        return None
    try:
        return PersonDetection(
            (float(box["x1"]), float(box["y1"]), float(box["x2"]), float(box["y2"])),
            float(score),
        )
    except (KeyError, TypeError, ValueError):
        return None
