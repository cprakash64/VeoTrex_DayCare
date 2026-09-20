"""Entry point for the recorded-video demonstration runtime.

Starts the real TensorRT worker, the real tracker, and a recorded source, then serves the
resulting state and annotated frames on loopback. If the GPU worker cannot start, the
runtime fails loudly instead of falling back to anything synthetic.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

import structlog

from veotrex_edge_agent.frame_source.recorded_video import (
    RecordedVideoConfig,
    RecordedVideoSource,
)
from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_inference import DetectionProfile, ReferenceImageDetector
from veotrex_edge_agent.monitoring.config import DemoRuntimeSettings
from veotrex_edge_agent.monitoring.occupancy import DemoStaffingPolicy
from veotrex_edge_agent.monitoring.pipeline import MonitoringPipeline
from veotrex_edge_agent.monitoring.server import make_server


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def build_pipeline(
    settings: DemoRuntimeSettings, supervisor: GpuWorkerSupervisor
) -> MonitoringPipeline:
    source = RecordedVideoSource(
        RecordedVideoConfig(
            path=settings.video_path,
            loop=settings.video_loop,
            cadence_fps=settings.cadence_fps,
            decoder=settings.decoder,
            jpeg_quality=settings.jpeg_quality,
        )
    )
    policy = (
        DemoStaffingPolicy(
            staff_on_duty=settings.staff_on_duty,
            people_per_staff=settings.people_per_staff,
        )
        if settings.staff_on_duty > 0
        else None
    )
    return MonitoringPipeline(
        source,
        ReferenceImageDetector(supervisor),
        area_label=settings.area_label,
        camera_label=settings.camera_label,
        policy=policy,
        stale_after_seconds=settings.stale_after_seconds,
        jpeg_quality=settings.jpeg_quality,
        detection_profile=DetectionProfile.TRACKING_HIGH_RECALL,
    )


def main() -> None:
    configure_logging()
    logger = structlog.get_logger()
    settings = DemoRuntimeSettings()
    supervisor = GpuWorkerSupervisor()
    supervisor.start()
    supervisor.load_model()
    pipeline = build_pipeline(settings, supervisor)
    pipeline.start()
    server = make_server(pipeline, host=settings.bind_host, port=settings.bind_port)
    stopping = threading.Event()

    def shutdown(_signum: int, _frame: FrameType | None) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, shutdown)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "demo_runtime_started",
        bind_host=settings.bind_host,
        bind_port=settings.bind_port,
        area=settings.area_label,
        source_kind=str(pipeline.snapshot().source_kind),
    )
    # Printed plainly, not only as a log field: this is the line the operator copies.
    print(f"OWNER_DEMO_URL=http://{settings.bind_host}:{settings.bind_port}/owner-demo", flush=True)
    try:
        stopping.wait()
    finally:
        server.shutdown()
        server.server_close()
        pipeline.stop()
        supervisor.stop()
        logger.info("demo_runtime_stopped")


if __name__ == "__main__":
    main()
