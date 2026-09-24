"""``veotrex-edge live-cameras`` and ``veotrex-edge live-demo`` (V1-DEMO-01).

Two commands, matching the operator flow for Sunday: find the camera, then run the demo.
Registered as subcommands of the existing edge CLI so there is still one entry point.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict
from typing import Any

from veotrex_edge_agent.live.camera import LocalCameraSource, discover_cameras
from veotrex_edge_agent.live.fake import FakeLiveSource
from veotrex_edge_agent.live.runtime import LiveDemoRuntime
from veotrex_edge_agent.live.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DemoServer,
    InsecureBindRefused,
)
from veotrex_edge_agent.live.source import LiveSourceError
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.recorded.yolox import DetectorUnavailable, YoloxPersonDetector

SOURCE_CHOICES = ("camera", "synthetic")
DETECTOR_CHOICES = ("yolox", "none")


def add_discover_arguments(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
    command.add_argument(
        "--json", action="store_true", help="machine-readable output instead of a table"
    )
    return command


def add_demo_arguments(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
    command.add_argument("--source", choices=SOURCE_CHOICES, default="camera")
    command.add_argument(
        "--device",
        default="0",
        help="camera index or /dev/videoN. Never assume 0; run live-cameras first",
    )
    command.add_argument("--width", type=int, default=1280)
    command.add_argument("--height", type=int, default=720)
    command.add_argument("--fps", type=float, default=30.0)
    command.add_argument("--detector", choices=DETECTOR_CHOICES, default="yolox")
    command.add_argument(
        "--environment",
        default="local",
        help="gate for the evaluation detector; staging and production are refused",
    )
    command.add_argument("--host", default=DEFAULT_HOST, help="dashboard bind address")
    command.add_argument("--port", type=int, default=DEFAULT_PORT)
    command.add_argument(
        "--allow-non-loopback-bind",
        action="store_true",
        help="serve the UNAUTHENTICATED dashboard off loopback. Prefer an SSH tunnel",
    )
    command.add_argument(
        "--headless", action="store_true", help="run the pipeline without the dashboard"
    )
    command.add_argument(
        "--max-frames", type=int, default=None, help="stop after this many processed frames"
    )
    command.add_argument(
        "--duration", type=float, default=None, help="stop after this many seconds"
    )
    return command


def run_discover_cli(arguments: argparse.Namespace) -> int:
    candidates = discover_cameras()
    if arguments.json:
        print(json.dumps([asdict(item) for item in candidates], indent=2, sort_keys=True))
        return 0 if any(item.usable for item in candidates) else 1
    if not candidates:
        print("No V4L2 capture devices found (/dev/video* is empty).")
        print("Attach a UVC camera, or run the demo with --source synthetic.")
        return 1
    print(f"{'device':<14} {'resolution':<12} {'fps':>6}  status")
    for item in candidates:
        geometry = f"{item.width}x{item.height}" if item.width else "-"
        fps = f"{item.fps:.0f}" if item.fps else "-"
        print(f"{item.device:<14} {geometry:<12} {fps:>6}  {item.detail}")
    usable = [item for item in candidates if item.usable]
    if usable:
        print(f"\nUse: veotrex-edge live-demo --device {usable[0].device}")
        return 0
    print("\nNo device could be opened for capture.")
    return 1


def _source(arguments: argparse.Namespace) -> Any:
    if arguments.source == "synthetic":
        # Declares itself SYNTHETIC_TEST, so the dashboard cannot present it as live.
        return FakeLiveSource(
            frame_count=arguments.max_frames or 10_000,
            width=arguments.width,
            height=arguments.height,
            fps=arguments.fps,
            interval_seconds=1.0 / max(arguments.fps, 1.0),
        )
    return LocalCameraSource(
        arguments.device, width=arguments.width, height=arguments.height, fps=arguments.fps
    )


def _detector(arguments: argparse.Namespace) -> Any:
    if arguments.detector == "none":
        return FakePersonDetector({})
    return YoloxPersonDetector(environment=arguments.environment)


def run_demo_cli(arguments: argparse.Namespace) -> int:
    try:
        source = _source(arguments)
    except LiveSourceError as exc:
        print(f"camera rejected: {exc.category}", file=sys.stderr)
        return 2
    try:
        detector = _detector(arguments)
    except DetectorUnavailable as exc:
        print(f"detector unavailable: {exc}", file=sys.stderr)
        return 2

    starting = getattr(detector, "start", None)
    if callable(starting):
        try:
            starting()
        except DetectorUnavailable as exc:
            print(f"detector unavailable: {exc}", file=sys.stderr)
            return 2

    runtime = LiveDemoRuntime(source, detector)
    server: DemoServer | None = None
    code = 0
    try:
        if not arguments.headless:
            try:
                server = DemoServer(
                    runtime,
                    host=arguments.host,
                    port=arguments.port,
                    allow_non_loopback=arguments.allow_non_loopback_bind,
                )
            except InsecureBindRefused as exc:
                print(f"refusing to bind: {exc}", file=sys.stderr)
                return 2
            host, port = server.start()
            print(f"Dashboard:  http://{host}:{port}/")
            print("From the demo laptop, tunnel it:")
            print(f"  ssh -N -L {port}:127.0.0.1:{port} {_ssh_hint()}")
            print("Press Ctrl-C to stop.\n")

        stopping = {"requested": False}

        def handle(*_: Any) -> None:
            stopping["requested"] = True
            runtime.stop()

        for signal_name in ("SIGINT", "SIGTERM"):
            with_signal = getattr(signal, signal_name, None)
            if with_signal is not None:
                signal.signal(with_signal, handle)

        if arguments.headless:
            runtime.run(max_frames=arguments.max_frames)
        else:
            runtime.start()
            deadline = time.monotonic() + arguments.duration if arguments.duration else None
            while runtime.running and not stopping["requested"]:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        if runtime.failure:
            print(f"stopped: {runtime.failure}", file=sys.stderr)
            code = 1
    finally:
        runtime.stop()
        if server is not None:
            server.stop()
        closing = getattr(detector, "close", None)
        if callable(closing):
            closing()

    print(
        json.dumps(
            {"metrics": runtime.metrics(), "failure": runtime.failure}, indent=2, sort_keys=True
        )
    )
    return code


def _ssh_hint() -> str:
    """A placeholder the operator replaces. No hostname or user is guessed or stored."""
    return "<user>@<jetson-host>"
