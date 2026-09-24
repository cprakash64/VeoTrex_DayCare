"""Live camera ingestion and the real-time tracking demo (V1-DEMO-01).

    LiveVideoSource -> BackpressureScheduler -> (B1A detector + tracker) -> live state

The source is replaceable and nothing downstream knows which one is running. A local USB
camera today and Ring WHEP once the upstream block clears are interchangeable, because the
frame contract - not the device - is what detection and tracking depend on.

Nothing here performs or enables face recognition.
"""

from veotrex_edge_agent.live.camera import (
    SOURCE_VIEWS,
    CameraCandidate,
    LocalCameraSource,
    crop_to_view,
    discover_cameras,
    list_device_nodes,
    probe_camera,
    validate_source_view,
)
from veotrex_edge_agent.live.fake import FakeLiveSource
from veotrex_edge_agent.live.preview import (
    PreviewBuffer,
    PreviewConfig,
    PreviewFrame,
    PreviewRenderer,
)
from veotrex_edge_agent.live.runtime import LiveDemoRuntime, LiveState, TrackBox
from veotrex_edge_agent.live.scheduler import BackpressureScheduler, SchedulerMetrics
from veotrex_edge_agent.live.source import (
    LiveFrame,
    LiveSourceError,
    LiveVideoSource,
    SourceDescription,
    SourceHealth,
    SourceKind,
    health_label,
)
from veotrex_edge_agent.live.timeline import DemoEvent, DemoEventKind, DemoTimeline

__all__ = [
    "SOURCE_VIEWS",
    "BackpressureScheduler",
    "CameraCandidate",
    "DemoEvent",
    "DemoEventKind",
    "DemoTimeline",
    "FakeLiveSource",
    "LiveDemoRuntime",
    "LiveFrame",
    "LiveSourceError",
    "LiveState",
    "LiveVideoSource",
    "LocalCameraSource",
    "PreviewBuffer",
    "PreviewConfig",
    "PreviewFrame",
    "PreviewRenderer",
    "SchedulerMetrics",
    "SourceDescription",
    "SourceHealth",
    "SourceKind",
    "TrackBox",
    "crop_to_view",
    "discover_cameras",
    "health_label",
    "list_device_nodes",
    "probe_camera",
    "validate_source_view",
]
