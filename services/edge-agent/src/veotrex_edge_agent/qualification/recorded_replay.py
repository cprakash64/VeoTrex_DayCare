from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_inference import DetectionProfile, ReferenceImageDetector
from veotrex_edge_agent.image_pipeline import TRANSFORM_VERSION, PixelFormat
from veotrex_edge_agent.qualification.image_decoder import decode_image
from veotrex_edge_agent.tracking import PersonDetection, PersonTracker, TrackingConfig

MODEL_SHA256 = "f204dff3573a15647266ba287f789d266fd95a912dd0a75e973ab046e3991068"


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    stream_instance_id: str
    frame_sequence: int
    source_timestamp: float
    width: int
    height: int
    pixel_format: PixelFormat
    decoded_path: Path


def extract_frames(video: Path, output: Path, cadence_fps: int) -> list[RecordedFrame]:
    if cadence_fps not in (2, 5, 8, 10, 15):
        raise ValueError("unsupported_qualification_cadence")
    output.mkdir(parents=True, exist_ok=True)
    pattern = output / "frame-%08d.jpg"
    discoverer = shutil.which("gst-discoverer-1.0")
    launcher = shutil.which("gst-launch-1.0")
    if discoverer is None or launcher is None:
        raise RuntimeError("gstreamer_tools_unavailable")
    discovery = subprocess.run(  # noqa: S603 - executable resolved from trusted PATH
        [discoverer, str(video)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    decoder = "vp9dec" if "video #1: VP9" in discovery else "vp8dec"
    command = [
        launcher,
        "-q",
        "filesrc",
        f"location={video}",
        "!",
        "matroskademux",
        "!",
        decoder,
        "!",
        "videorate",
        "!",
        f"video/x-raw,framerate={cadence_fps}/1",
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        "quality=95",
        "!",
        "multifilesink",
        f"location={pattern}",
    ]
    subprocess.run(  # noqa: S603 - executable resolved from trusted PATH; argv only
        command, check=True, stdin=subprocess.DEVNULL, timeout=900
    )
    frames = []
    for sequence, path in enumerate(sorted(output.glob("frame-*.jpg"))):
        decoded = decode_image(path)
        height, width, _ = decoded.rgb.shape
        frames.append(
            RecordedFrame(
                video.stem,
                sequence,
                sequence / cadence_fps,
                width,
                height,
                PixelFormat.RGB8,
                path,
            )
        )
    return frames


def _detection(item: dict[str, object]) -> PersonDetection | None:
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


def run_production_replay(
    video: Path,
    frames: list[RecordedFrame],
    output: Path,
    *,
    config: TrackingConfig | None = None,
) -> dict[str, Any]:
    config = config or TrackingConfig()
    supervisor = GpuWorkerSupervisor()
    detector = ReferenceImageDetector(supervisor)
    tracker = PersonTracker(config)
    results: list[dict[str, Any]] = []
    supervisor.start()
    supervisor.load_model()
    started = time.perf_counter()
    try:
        for frame in frames:
            decoded = decode_image(frame.decoded_path)
            inference = detector.infer_decoded(
                decoded.rgb,
                pixel_format=frame.pixel_format,
                frame_id=f"{frame.stream_instance_id}-{frame.frame_sequence}",
                profile=DetectionProfile.TRACKING_HIGH_RECALL,
            )
            detections = [value for item in inference["detections"] if (value := _detection(item))]
            tracked = tracker.update(
                frame.stream_instance_id,
                frame.frame_sequence,
                frame.source_timestamp,
                detections,
                source_width=frame.width,
                source_height=frame.height,
            )
            results.append(
                {
                    "sequence": frame.frame_sequence,
                    "timestamp": frame.source_timestamp,
                    "detections": [asdict(x) for x in detections],
                    "tracking": asdict(tracked),
                    "timing": inference["image_timing"],
                }
            )
    finally:
        worker_status = supervisor.status()
        supervisor.stop()
    payload = {
        "source_video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "model_sha256": MODEL_SHA256,
        "preprocessing_version": TRANSFORM_VERSION,
        "profile": DetectionProfile.TRACKING_HIGH_RECALL,
        "configuration": asdict(config),
        "frames": results,
        "worker_restart_count": worker_status["restart_count"],
        "wall_seconds": time.perf_counter() - started,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["cache_sha256"] = hashlib.sha256(encoded).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload
