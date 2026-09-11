from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_inference import DetectionProfile, ReferenceImageDetector
from veotrex_edge_agent.image_pipeline import PixelFormat
from veotrex_edge_agent.qualification.image_decoder import decode_image
from veotrex_edge_agent.qualification.recorded_replay import _detection
from veotrex_edge_agent.tracking import PersonTracker, TrackingConfig


def _process(pid: int) -> tuple[int, int]:
    lines = Path(f"/proc/{pid}/status").read_text().splitlines()
    rss = int(next(x for x in lines if x.startswith("VmRSS:")).split()[1])
    return rss, len(list(Path(f"/proc/{pid}/fd").iterdir()))


def _summary(values: list[float]) -> dict[str, float]:
    return {"minimum": min(values), "maximum": max(values), "mean": statistics.mean(values)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=900)
    args = parser.parse_args()
    paths = sorted(args.frames.glob("frame-*.jpg"))
    if not paths:
        parser.error("no replay frames")
    supervisor = GpuWorkerSupervisor()
    detector = ReferenceImageDetector(supervisor)
    tracker = PersonTracker(TrackingConfig(max_lost_seconds=2.0))
    values: dict[str, list[float]] = {
        key: []
        for key in (
            "latency_ms",
            "tracking_ms",
            "parent_rss_kib",
            "worker_rss_kib",
            "parent_fds",
            "worker_fds",
            "active_tracks",
            "lost_tracks",
            "history_samples",
        )
    }
    failures: list[str] = []
    iterations = cycles = 0
    started = time.monotonic()
    supervisor.start()
    supervisor.load_model()
    worker_pid = supervisor.pid
    assert worker_pid is not None
    try:
        while time.monotonic() - started < args.duration_seconds:
            if iterations and iterations % len(paths) == 0:
                tracker.reset("tracking-soak")
                cycles += 1
            step = time.perf_counter_ns()
            try:
                decoded = decode_image(paths[iterations % len(paths)])
                result = detector.infer_decoded(
                    decoded.rgb,
                    pixel_format=PixelFormat.RGB8,
                    frame_id=f"soak-{iterations}",
                    profile=DetectionProfile.TRACKING_HIGH_RECALL,
                )
                detections = [x for item in result["detections"] if (x := _detection(item))]
                height, width, _ = decoded.rgb.shape
                tracked = tracker.update(
                    "tracking-soak",
                    iterations % len(paths),
                    (iterations % len(paths)) / 5,
                    detections,
                    source_width=width,
                    source_height=height,
                )
            except Exception as exc:
                failures.append(type(exc).__name__)
                break
            parent, worker = _process(os.getpid()), _process(worker_pid)
            status = tracker.status("tracking-soak")
            values["latency_ms"].append((time.perf_counter_ns() - step) / 1e6)
            values["tracking_ms"].append(tracked.tracking_update_ms)
            values["parent_rss_kib"].append(float(parent[0]))
            values["worker_rss_kib"].append(float(worker[0]))
            values["parent_fds"].append(float(parent[1]))
            values["worker_fds"].append(float(worker[1]))
            for key, target in (
                ("active_tracks", "active_tracks"),
                ("lost_tracks", "lost_tracks"),
                ("history_samples", "retained_history_samples"),
            ):
                values[key].append(float(status[target]))
            iterations += 1
    finally:
        worker_status = supervisor.status()
        supervisor.stop()
    tenth = max(1, len(values["latency_ms"]) // 10)
    report = {
        "duration_seconds": time.monotonic() - started,
        "processed_frames": iterations,
        "completed_replay_cycles": cycles,
        "failures": failures,
        "worker_restart_count": worker_status["restart_count"],
        "latency_first_tenth_mean_ms": statistics.mean(values["latency_ms"][:tenth]),
        "latency_last_tenth_mean_ms": statistics.mean(values["latency_ms"][-tenth:]),
        "metrics": {key: _summary(value) for key, value in values.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))
    return int(bool(failures or worker_status["restart_count"]))


if __name__ == "__main__":
    raise SystemExit(main())
