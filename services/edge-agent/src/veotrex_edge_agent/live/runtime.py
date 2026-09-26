"""The live demo runtime (V1-DEMO-01).

Wires a live source to the detection and tracking stack that already exists:

    LiveVideoSource -> BackpressureScheduler (+ AdaptiveInferencePacer) ->
        RecordedTrackingPipeline -> live state

The pipeline is B1A's, unchanged. ``RecordedTrackingPipeline.process`` consumes any object
exposing ``frame_index`` / ``timestamp_ms`` / ``width`` / ``height`` / ``image``, and
``LiveFrame`` is a superset of exactly that, so the whole detector + bounding-box validation +
tracker + lifecycle + metrics chain is reused without a fork and without an edit. Only the
things that are genuinely new live here: occupancy, the timeline, source health and the
capture-side metrics.

**Inference is sampled, not exhaustive (V1-03A).** With an ``InferenceRateConfig`` the runtime
runs the detector on the newest frame at a paced, adaptive rate instead of on every frame it can
reach. The tracker only ever sees frames that were actually detected on, each with its own
capture-side timestamp, so a skipped camera frame neither compresses time nor counts as a miss:
the tracker's lost-track tolerance is in seconds, not frames. Nothing is interpolated and no box
is synthesised between detections.

**Occupancy counts evidence, not candidates (V1-03B).** Every confirmed track is shown, but only
one whose evidence the ``OccupancyLedger`` has validated changes the head count or produces
``PERSON_APPEARED_IN_VIEW`` / ``PERSON_NO_LONGER_VISIBLE``. A track that has not yet earned that
is an ``OCCUPANCY_CANDIDATE``: drawn, counted separately, explained in the diagnostics, and never
silently discarded. See ``occupancy``.

No recognition of any kind runs. Nothing in this module imports the face stack, and a test
asserts the package cannot reach it. Everyone in view is an anonymous track, which is what
makes the demo honest: an unidentified person is simply a person, never a "child" and never an
"unknown intruder".
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from veotrex_edge_agent.live.occupancy import (
    OCCUPANCY_CANDIDATE,
    OCCUPANCY_VALIDATED,
    OccupancyEvidencePolicy,
    OccupancyLedger,
)
from veotrex_edge_agent.live.preview import PreviewRenderer
from veotrex_edge_agent.live.scheduler import (
    AdaptiveInferencePacer,
    BackpressureScheduler,
    InferenceRateConfig,
)
from veotrex_edge_agent.live.source import (
    LiveSourceError,
    SourceHealth,
    SourceKind,
    health_label,
)
from veotrex_edge_agent.live.timeline import DemoEventKind, DemoTimeline
from veotrex_edge_agent.recorded.model import TrackLifecycle
from veotrex_edge_agent.recorded.pipeline import RecordedTrackingPipeline
from veotrex_edge_agent.recorded.regions import IgnoreRegionSet
from veotrex_edge_agent.tracking import TrackingConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_edge_agent.live.source import LiveVideoSource
    from veotrex_edge_agent.recorded.detector import PersonDetector

# How many boxes the dashboard is willing to draw. A frame with more people than this is a
# detector malfunction, and the dashboard should stay responsive rather than render them all.
MAX_RENDERED_TRACKS = 32


@dataclass(slots=True)
class TrackBox:
    """One confirmed track as the dashboard needs it: a box, an id and whether it counts."""

    track_id: int
    bbox_xyxy: tuple[float, float, float, float]
    confidence: float
    occupancy_status: str = OCCUPANCY_CANDIDATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "bbox_xyxy": [round(value, 1) for value in self.bbox_xyxy],
            "confidence": round(self.confidence, 3),
            "occupancy_status": self.occupancy_status,
        }


@dataclass(slots=True)
class LiveState:
    """The current picture, replaced whole on every processed frame.

    Read by the dashboard from another thread, so it is swapped under a lock rather than
    mutated in place - a half-updated state would render boxes from one frame with the
    occupancy of another.
    """

    frame_index: int = 0
    timestamp_ms: float = 0.0
    width: int = 0
    height: int = 0
    # Validated tracks only. Candidates are reported next to it, never inside it.
    occupancy: int = 0
    candidate_tracks: int = 0
    tracks: list[TrackBox] = field(default_factory=list)
    source_kind: str = ""
    source_id: str = ""
    source_health: str = str(SourceHealth.STARTING)
    is_live: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "timestamp_ms": round(self.timestamp_ms, 1),
            "width": self.width,
            "height": self.height,
            "occupancy": self.occupancy,
            "candidate_tracks": self.candidate_tracks,
            "tracks": [track.as_dict() for track in self.tracks],
            "source": {
                "kind": self.source_kind,
                "id": self.source_id,
                # The machine-readable state and the words a human reads are different
                # things. Both are sent, so the page never has to invent the wording.
                "health": self.source_health,
                "health_label": health_label(self.source_health),
                "is_live": self.is_live,
            },
        }


class LiveDemoRuntime:
    """Owns one live session: source, scheduler, pipeline, state and timeline."""

    def __init__(
        self,
        source: LiveVideoSource,
        detector: PersonDetector,
        *,
        tracking_config: TrackingConfig | None = None,
        capacity: int = 1,
        timeline: DemoTimeline | None = None,
        preview: PreviewRenderer | None = None,
        ignore_regions: IgnoreRegionSet | None = None,
        inference_rate: InferenceRateConfig | None = None,
        occupancy_policy: OccupancyEvidencePolicy | None = None,
    ) -> None:
        # Optional on purpose: --headless and the automated tests run the whole pipeline with
        # no preview at all, so nothing about detection or tracking depends on it existing.
        self.preview = preview
        self._source = source
        self._detector = detector
        # No rate means unpaced: the consumer takes the newest frame whenever it is free. The
        # live-demo CLI always passes a rate; tests that reason about exact frame sequences
        # rely on the unpaced form.
        self.inference_rate = inference_rate
        self._scheduler = BackpressureScheduler(
            source,
            capacity=capacity,
            pacer=None if inference_rate is None else AdaptiveInferencePacer(inference_rate),
        )
        self._pipeline = RecordedTrackingPipeline(
            detector, tracking_config=tracking_config, ignore_regions=ignore_regions
        )
        self.ignore_regions = self._pipeline.ignore_regions
        effective_tracking = tracking_config or TrackingConfig()
        self.occupancy = OccupancyLedger(
            occupancy_policy or OccupancyEvidencePolicy.from_tracking(effective_tracking)
        )
        # The observations a track made while TENTATIVE never reach the runtime. The tracker
        # only lets high-score detections create or confirm a track, so all of them were high.
        self._pre_confirmation_high = max(0, effective_tracking.confirmation_observations - 1)
        self.timeline = timeline or DemoTimeline()
        self._state = LiveState(
            source_kind=str(source.kind),
            source_id=source.source_id,
            is_live=SourceKind(source.kind).is_live,
        )
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------------- state
    @property
    def state(self) -> LiveState:
        with self._lock:
            return self._state

    @property
    def failure(self) -> str | None:
        return self._failure or self._scheduler.failure

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _session_ms(self) -> float:
        return (time.monotonic() - self._started_monotonic) * 1000.0

    def metrics(self) -> dict[str, Any]:
        """Capture-side and pipeline-side numbers, merged into one snapshot.

        ``processing_fps`` is recomputed here rather than taken from the pipeline. The
        pipeline finalises its own elapsed time in a ``finally``, which is correct for a
        recorded file that runs to completion and useless for a live session: it would read
        0.0 on the dashboard for as long as the demo was actually running. It is the same
        number as ``effective_inference_fps``, measured from the first frame taken for
        inference, so seconds spent negotiating a camera session do not dilute it.

        Frame accounting, by cause (see ``scheduler`` for the exact rules):

        ``camera_frames_captured_total``                 every frame the source delivered
        ``inference_frames_selected_total``              taken for inference
        ``inference_frames_processed_total``             detection + tracking completed
        ``inference_frames_skipped_scheduler_total``     intentionally not sampled
        ``inference_frames_dropped_backpressure_total``  wanted, but the detector was busy
        ``frames_dropped_total``                         same as the line above
        ``source_frames_dropped_total``                  lost before capture; None when the
                                                         source cannot measure it
        """
        snapshot = self._pipeline.metrics.snapshot()
        snapshot.update(self._scheduler.snapshot())
        elapsed = max(time.monotonic() - self._started_monotonic, 1e-9)
        processed = self._pipeline.metrics.video_frames_processed_total
        snapshot["inference_frames_processed_total"] = processed
        snapshot["processing_fps"] = snapshot["effective_inference_fps"]
        snapshot["session_seconds"] = round(elapsed, 2)
        snapshot["camera_reconnect_count"] = getattr(self._source, "reconnect_count", 0)
        dropped_upstream = getattr(self._source, "media_frames_dropped_total", None)
        snapshot["source_frames_dropped_total"] = (
            dropped_upstream if isinstance(dropped_upstream, int) else None
        )
        snapshot["occupancy"] = self.timeline.occupancy
        snapshot["peak_occupancy"] = self.timeline.peak_occupancy
        snapshot["source_health"] = str(self._source.health)
        snapshot.update(self.occupancy.snapshot())
        snapshot["ignore_regions_configured"] = len(self.ignore_regions)
        if self.preview is not None:
            snapshot.update(self.preview.snapshot())
        return snapshot

    def occupancy_diagnostics(self) -> dict[str, Any]:
        """Per-track evidence for operator review: normalised geometry and scores, no pixels."""
        return self.occupancy.diagnostics()

    def calibration(self) -> dict[str, Any]:
        """The configured ignore regions and what they have suppressed. Nothing is hidden."""
        status = self.ignore_regions.status()
        status["detections_suppressed_total"] = self._pipeline.metrics.detections_ignored_total
        return status

    # ------------------------------------------------------------------------- processing
    def _consume(self, max_frames: int | None = None) -> None:
        """Drive the pipeline over scheduled frames until capture stops.

        State is published once per processed frame, from that frame's own confirmed boxes, and
        again whenever a track ends. Occupancy is the number of *validated* tracks that have not
        yet ended - the same lifecycle the timeline narrates - so one missed detection inside the
        tracker's tolerance changes neither the count nor the timeline, and a candidate changes
        neither at all. Boxes are drawn only where the detector actually found someone on this
        frame; nothing is predicted or interpolated onto the picture.
        """
        live_boxes: dict[int, TrackBox] = {}
        confidences: dict[int, float] = {}
        current_frame: Any = None

        def hook(image: Any, boxes: Any, timestamp_ms: float) -> None:
            nonlocal live_boxes
            bounded = list(boxes)[:MAX_RENDERED_TRACKS]
            statuses = self.occupancy.statuses()
            live_boxes = {
                int(track_id): TrackBox(
                    int(track_id),
                    tuple(box),
                    confidences.get(int(track_id), 0.0),
                    statuses.get(int(track_id), OCCUPANCY_CANDIDATE),
                )
                for track_id, box in bounded
            }
            if self.preview is not None:
                # Drawn here because this is the one place that holds the processed frame and
                # its own confirmed boxes together; geometry and image cannot drift apart, and
                # no second capture, detection or client-side redraw is involved.
                self.preview.render(
                    image,
                    bounded,
                    frame_index=getattr(current_frame, "frame_index", 0),
                    occupancy=self.occupancy.validated_count,
                    source_health=str(self._source.health),
                    candidates=frozenset(
                        track_id
                        for track_id, box in live_boxes.items()
                        if box.occupancy_status != OCCUPANCY_VALIDATED
                    ),
                    regions=self.ignore_regions,
                )
            self._publish(current_frame, live_boxes)

        def frames() -> Any:
            nonlocal current_frame
            for frame in self._scheduler.frames(max_frames=max_frames):
                if self._stop.is_set():
                    return
                current_frame = frame
                confidences.clear()
                yield frame

        self.timeline.record(DemoEventKind.TRACKING_STARTED, session_ms=self._session_ms())
        if SourceKind(self._source.kind).is_live:
            self.timeline.record(DemoEventKind.CAMERA_CONNECTED, session_ms=self._session_ms())
        try:
            for record in self._pipeline.process(
                frames(), run_id=self._source.source_id, frame_hook=hook
            ):
                observation = record.observation
                if observation is not None:
                    if observation.lifecycle is TrackLifecycle.TRACK_STARTED:
                        self.occupancy.start(
                            observation.track_id,
                            prior_high_observations=self._pre_confirmation_high,
                        )
                    became_validated = self.occupancy.observe(
                        observation.track_id,
                        score=observation.detection_confidence,
                        bbox=observation.bbox_xyxy,
                        width=getattr(current_frame, "width", 0),
                        height=getattr(current_frame, "height", 0),
                        timestamp_ms=observation.timestamp_ms,
                    )
                    if became_validated:
                        # Appearance is announced when the track starts to count, not when the
                        # tracker first confirms it: a candidate is not an arrival.
                        self.timeline.record(
                            DemoEventKind.PERSON_APPEARED_IN_VIEW,
                            session_ms=self._session_ms(),
                            track_id=observation.track_id,
                        )
                    # Confidence comes from the observation rather than the draw hook, which
                    # only carries geometry. The hook for this frame runs after its records.
                    confidences[observation.track_id] = observation.detection_confidence
                summary = record.summary
                if summary is not None:
                    ended = self.occupancy.end(summary.track_id)
                    if ended is not None and ended.validated:
                        self.timeline.record(
                            DemoEventKind.PERSON_NO_LONGER_VISIBLE,
                            session_ms=self._session_ms(),
                            track_id=summary.track_id,
                        )
                    live_boxes.pop(summary.track_id, None)
                    # Also reached after the last frame, when the stream ends with tracks
                    # still live and no further hook will run.
                    self._publish(current_frame, live_boxes)
        except LiveSourceError as exc:
            self._failure = exc.category
        except Exception:
            self._failure = "pipeline_error"
            self._logger.warning("live_pipeline_failed")
        finally:
            if SourceKind(self._source.kind).is_live and self._source.health in {
                SourceHealth.FAILED,
                SourceHealth.STOPPED,
            }:
                self.timeline.record(
                    DemoEventKind.CAMERA_DISCONNECTED, session_ms=self._session_ms()
                )
            self.timeline.record(DemoEventKind.TRACKING_STOPPED, session_ms=self._session_ms())
            if self.preview is not None:
                # A frozen last frame would keep looking live after the camera has gone.
                self.preview.clear()

    def _publish(self, frame: Any, boxes: dict[int, TrackBox]) -> None:
        """Swap in a fresh state. Occupancy counts validated, not-yet-ended tracks only."""
        occupancy = self.occupancy.validated_count
        candidates = self.occupancy.candidate_count
        event = self.timeline.set_occupancy(occupancy, session_ms=self._session_ms())
        if event is not None:
            self._logger.info("live_occupancy_changed", occupancy=occupancy)
        state = LiveState(
            frame_index=getattr(frame, "frame_index", 0),
            timestamp_ms=getattr(frame, "timestamp_ms", 0.0),
            width=getattr(frame, "width", 0),
            height=getattr(frame, "height", 0),
            occupancy=occupancy,
            candidate_tracks=candidates,
            tracks=sorted(boxes.values(), key=lambda item: item.track_id),
            source_kind=str(self._source.kind),
            source_id=self._source.source_id,
            source_health=str(self._source.health),
            is_live=SourceKind(self._source.kind).is_live,
        )
        with self._lock:
            self._state = state

    # ------------------------------------------------------------------------- lifecycle
    def run(self, *, max_frames: int | None = None) -> None:
        """Process synchronously until the source stops. Used by tests and by ``--headless``."""
        try:
            self._consume(max_frames=max_frames)
        finally:
            self.stop()

    def start(self) -> None:
        """Process on a background thread so an HTTP server can serve the state."""
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._consume, name="veotrex-live-runtime", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop processing and release everything. Idempotent."""
        self._stop.set()
        with contextlib.suppress(Exception):
            self._scheduler.stop()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def __enter__(self) -> LiveDemoRuntime:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
