from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.image_pipeline import PixelFormat, add_source_coordinates, preprocess_image
from veotrex_edge_agent.qualification.image_decoder import decode_image


def _process_sample(pid: int) -> tuple[int, int]:
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    rss_kib = int(next(x for x in status if x.startswith("VmRSS:")).split()[1])
    return rss_kib, len(list(Path(f"/proc/{pid}/fd").iterdir()))


def _available_memory_kib() -> int:
    lines = Path("/proc/meminfo").read_text().splitlines()
    return int(next(x for x in lines if x.startswith("MemAvailable:")).split()[1])


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "minimum": min(values),
        "maximum": max(values),
        "mean": statistics.mean(values),
        "p50": ordered[len(ordered) // 2],
        "p95": ordered[int(len(ordered) * 0.95)],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=600.0)
    parser.add_argument("--image-count", type=int, default=50)
    args = parser.parse_args()
    paths = sorted(args.images.glob("*.jpg"))[: args.image_count]
    if not paths or args.duration_seconds <= 0:
        parser.error("images and a positive duration are required")

    supervisor = GpuWorkerSupervisor()
    latencies: list[float] = []
    parent_rss: list[float] = []
    worker_rss: list[float] = []
    parent_fds: list[float] = []
    worker_fds: list[float] = []
    available_memory: list[float] = []
    failures: list[str] = []
    started = time.monotonic()
    supervisor.start()
    supervisor.load_model()
    worker_pid = supervisor.pid
    assert worker_pid is not None
    iterations = 0
    try:
        while time.monotonic() - started < args.duration_seconds:
            step = time.perf_counter_ns()
            try:
                decoded = decode_image(paths[iterations % len(paths)])
                prepared = preprocess_image(decoded.rgb, pixel_format=PixelFormat.RGB8)
                result = supervisor.infer_tensor(
                    prepared.tensor.tobytes(), frame_id=f"soak-{iterations}"
                )
                add_source_coordinates(result["detections"], prepared.transform)
            except Exception as exc:  # qualification must preserve the failure category
                failures.append(type(exc).__name__)
                break
            latencies.append((time.perf_counter_ns() - step) / 1e6)
            parent = _process_sample(os.getpid())
            worker = _process_sample(worker_pid)
            parent_rss.append(float(parent[0]))
            parent_fds.append(float(parent[1]))
            worker_rss.append(float(worker[0]))
            worker_fds.append(float(worker[1]))
            available_memory.append(float(_available_memory_kib()))
            iterations += 1
    finally:
        status = supervisor.status()
        supervisor.stop()
    tenth = max(1, len(latencies) // 10)
    report: dict[str, Any] = {
        "duration_seconds": time.monotonic() - started,
        "successful_images": iterations,
        "failures": failures,
        "worker_pid": worker_pid,
        "worker_restart_count": status["restart_count"],
        "latency_ms": _summary(latencies),
        "latency_first_tenth_mean_ms": statistics.mean(latencies[:tenth]),
        "latency_last_tenth_mean_ms": statistics.mean(latencies[-tenth:]),
        "parent_rss_kib": _summary(parent_rss),
        "worker_rss_kib": _summary(worker_rss),
        "combined_rss_kib": _summary([a + b for a, b in zip(parent_rss, worker_rss, strict=True)]),
        "parent_fd_count": _summary(parent_fds),
        "worker_fd_count": _summary(worker_fds),
        "available_memory_kib": _summary(available_memory),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))
    return int(bool(failures or status["restart_count"]))


if __name__ == "__main__":
    raise SystemExit(main())
