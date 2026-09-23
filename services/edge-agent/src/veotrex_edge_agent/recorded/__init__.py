"""Recorded-video person detection and tracking (V1-02B1A).

The substrate every later temporal feature depends on:

    video -> detection -> tracking -> (later) identity annotation -> (later) events

DETECTION != TRACKING != IDENTITY != EVENT. A detector box is one observation. A track is
temporal continuity between observations, anonymous by construction. Teacher identity is an
optional annotation supported only for enrolled, consenting staff. An event is business logic
derived later from temporal evidence. Nothing in this package produces an identity or an event.
"""

from veotrex_edge_agent.recorded.detector import (
    BoundingBoxValidator,
    DetectorError,
    FakePersonDetector,
    PersonDetector,
)
from veotrex_edge_agent.recorded.model import (
    TRACK_SCHEMA_VERSION,
    DetectedPerson,
    TrackEndReason,
    TrackIdentityObservation,
    TrackLifecycle,
    TrackObservation,
    TrackSummary,
)
from veotrex_edge_agent.recorded.pipeline import (
    PipelineMetrics,
    PipelineRecord,
    RecordedTrackingPipeline,
)
from veotrex_edge_agent.recorded.runner import RunResult, run_recorded_tracking
from veotrex_edge_agent.recorded.source import (
    RecordedVideoSource,
    VideoFrame,
    VideoMetadata,
    VideoSourceError,
)

__all__ = [
    "TRACK_SCHEMA_VERSION",
    "BoundingBoxValidator",
    "DetectedPerson",
    "DetectorError",
    "FakePersonDetector",
    "PersonDetector",
    "PipelineMetrics",
    "PipelineRecord",
    "RecordedTrackingPipeline",
    "RecordedVideoSource",
    "RunResult",
    "TrackEndReason",
    "TrackIdentityObservation",
    "TrackLifecycle",
    "TrackObservation",
    "TrackSummary",
    "VideoFrame",
    "VideoMetadata",
    "VideoSourceError",
    "run_recorded_tracking",
]
