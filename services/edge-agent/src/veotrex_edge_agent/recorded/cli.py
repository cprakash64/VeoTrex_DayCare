"""``veotrex-edge track-recording`` - process one local recording (V1-02B1A).

Registered as a subcommand of the existing edge CLI rather than as a second console script,
so an operator keeps one entry point for edge work.

The detector is chosen explicitly. ``--detector yolox`` is the real TensorRT path and is
refused outside local/development/test/ci; ``--detector none`` runs the whole pipeline with a
detector that finds nobody, which is how an operator checks that a file decodes and that
timestamps are sane before spending GPU time on it.

Local files only. There is no URL option, and adding one would need a separate decision about
what the edge may fetch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from veotrex_edge_agent.recorded.detector import FakePersonDetector, PersonDetector
from veotrex_edge_agent.recorded.output import OutputError
from veotrex_edge_agent.recorded.runner import run_recorded_tracking
from veotrex_edge_agent.recorded.source import VideoSourceError
from veotrex_edge_agent.recorded.yolox import DetectorUnavailable, YoloxPersonDetector

DETECTOR_CHOICES = ("yolox", "none")


def add_arguments(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
    command.add_argument("--input", type=Path, required=True, help="local .mp4/.mov to process")
    command.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory for tracks.ndjson (created 0700; refuses to overwrite)",
    )
    command.add_argument("--detector", choices=DETECTOR_CHOICES, default="yolox")
    command.add_argument(
        "--environment",
        default="local",
        help="gate for the evaluation detector; staging and production are refused",
    )
    command.add_argument(
        "--sample-every",
        type=int,
        default=1,
        help="process every Nth frame. Timestamps stay on the source timeline regardless",
    )
    command.add_argument(
        "--max-frames", type=int, default=None, help="stop after this many processed frames"
    )
    command.add_argument(
        "--annotate",
        action="store_true",
        help="also write annotated.mp4 with boxes and track ids (local evaluation only)",
    )
    command.add_argument("--run-id", default=None)
    return command


def _detector(arguments: argparse.Namespace) -> PersonDetector:
    if arguments.detector == "none":
        # A detector that finds nobody. Exercises decode, timestamps, output and metrics
        # without a model, and is the reason this command is useful on a machine with no GPU.
        return FakePersonDetector({})
    return YoloxPersonDetector(environment=arguments.environment)


def run_cli(arguments: argparse.Namespace) -> int:
    if arguments.sample_every < 1:
        print("--sample-every must be at least 1", file=sys.stderr)
        return 2
    try:
        detector = _detector(arguments)
    except DetectorUnavailable as exc:
        print(f"detector unavailable: {exc}", file=sys.stderr)
        return 2

    started: Any = getattr(detector, "start", None)
    detector_info: dict[str, Any] = {}
    try:
        if callable(started):
            detector_info = started()
    except DetectorUnavailable as exc:
        print(f"detector unavailable: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_recorded_tracking(
            arguments.input,
            arguments.output_dir,
            detector,
            sample_every=arguments.sample_every,
            max_frames=arguments.max_frames,
            annotate=arguments.annotate,
            run_id=arguments.run_id,
        )
    except VideoSourceError as exc:
        print(f"video rejected: {exc.category}", file=sys.stderr)
        return 2
    except OutputError as exc:
        print(f"output rejected: {exc.category}", file=sys.stderr)
        return 2
    finally:
        # The GPU worker is a child process; it is stopped on every path, including a video
        # that turned out to be unreadable after the worker had already started.
        closing = getattr(detector, "close", None)
        if callable(closing):
            closing()

    summary = {
        "run_id": result.run_id,
        "tracks": str(result.tracks_path),
        "annotated": str(result.annotated_path) if result.annotated_path else None,
        "records": result.records_written,
        "unique_tracks": result.unique_tracks,
        "frames_processed": result.frames_processed,
        "detector": {
            "id": detector.model_id,
            "version": detector.model_version,
            **{k: v for k, v in detector_info.items() if k != "model_id"},
        },
        "metrics": result.metrics,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
