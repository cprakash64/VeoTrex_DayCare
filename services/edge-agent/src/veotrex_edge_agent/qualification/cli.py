from __future__ import annotations

import argparse
import asyncio
import getpass
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.qualification.backend import QualificationEnvironmentError
from veotrex_edge_agent.qualification.environment import environment_json, inspect_environment
from veotrex_edge_agent.qualification.gstreamer import GStreamerQualificationBackend
from veotrex_edge_agent.qualification.metrics import continuity_summary, decide
from veotrex_edge_agent.qualification.models import (
    CameraQualificationResult,
    CameraTarget,
    QualificationMode,
    SessionClass,
    SessionRequest,
)
from veotrex_edge_agent.qualification.orchestrator import (
    QualificationOrchestrator,
    SequentialConfig,
)
from veotrex_edge_agent.qualification.reporting import (
    build_report,
    human_summary,
    write_json_report,
)
from veotrex_edge_agent.qualification.resources import ResourceCollector
from veotrex_edge_agent.qualification.transport_qualification import (
    SCENARIOS,
    run_transport_cli,
)
from veotrex_edge_agent.qualification.webrtc_qualification import (
    SCENARIOS as WEBRTC_SCENARIOS,
)
from veotrex_edge_agent.qualification.webrtc_qualification import (
    run_webrtc_cli,
)


class ManualPromptTokenProvider:
    """Development-only, in-memory credential source. It never accepts argv or environment input."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def access_token_for(self, _request: SessionRequest) -> SecretStr:
        return self._token


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="veotrex-edge")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("qualify-env", help="read-only media environment inspection")
    qualify = commands.add_parser(
        "qualify-ring", help="DEVELOPMENT QUALIFICATION ONLY; does not retain media"
    )
    qualify.add_argument("--camera-id", type=UUID, action="append", required=True)
    qualify.add_argument(
        "--mode", type=QualificationMode, choices=list(QualificationMode), required=True
    )
    qualify.add_argument(
        "--session-class", type=SessionClass, choices=list(SessionClass), required=True
    )
    qualify.add_argument("--cycles", type=int, default=3)
    qualify.add_argument("--max-retries", type=int, default=1)
    qualify.add_argument("--decoder", choices=("auto", "software", "nvidia"), default="auto")
    qualify.add_argument("--stall-timeout", type=float, default=5.0)
    qualify.add_argument("--manual-prompt", action="store_true", required=True)
    qualify.add_argument("--concurrency", type=int, choices=(1, 2, 4, 8, 12))
    qualify.add_argument("--ramp-seconds", type=float, default=1.0)
    qualify.add_argument("--overlap-lead", type=float)
    qualify.add_argument("--report-dir", type=Path, default=Path("reports/qualification"))
    transport = commands.add_parser(
        "qualify-transport",
        help="R5A transport qualification on a synthetic loopback fixture; retains no media",
    )
    transport.add_argument("--scenario", choices=SCENARIOS, required=True)
    transport.add_argument("--codec", choices=("h264", "h265"), default="h264")
    transport.add_argument("--width", type=int, choices=(640, 1280, 1920), default=1920)
    transport.add_argument("--height", type=int, choices=(360, 720, 1080), default=1080)
    transport.add_argument("--fps", type=int, choices=(10, 15, 20, 25, 30), default=15)
    transport.add_argument("--duration", type=float)
    transport.add_argument("--report-dir", type=Path, default=Path("reports/qualification"))
    webrtc = commands.add_parser(
        "qualify-webrtc",
        help="R5A-R2 local WebRTC media qualification; synthetic media only, retains nothing",
    )
    webrtc.add_argument("--scenario", choices=WEBRTC_SCENARIOS, required=True)
    webrtc.add_argument("--codec", choices=("H264", "H265"), default="H264")
    webrtc.add_argument("--width", type=int, choices=(320, 640, 1280, 1920), default=1280)
    webrtc.add_argument("--height", type=int, choices=(240, 360, 720, 1080), default=720)
    webrtc.add_argument("--fps", type=int, choices=(5, 10, 15, 20, 25, 30), default=15)
    webrtc.add_argument("--duration", type=float)
    webrtc.add_argument("--report-dir", type=Path, default=Path("reports/qualification"))
    return root


def _prompt_manual_targets(  # pragma: no cover - deliberate no-echo operator interaction
    camera_ids: list[UUID],
) -> tuple[list[CameraTarget], SecretStr]:
    print(
        "DEVELOPMENT QUALIFICATION ONLY. No media is retained. "
        "Production deployments must inject the Stage 1B credential service.",
        file=sys.stderr,
    )
    targets: list[CameraTarget] = []
    for camera_id in camera_ids:
        provider_device = getpass.getpass(
            f"Ring provider device ID for camera-{camera_id.hex[:8]} (hidden): "
        ).strip()
        component = getpass.getpass("Ring component ID, blank for default (hidden): ").strip()
        if not provider_device:
            raise QualificationEnvironmentError("manual target is required")
        targets.append(
            CameraTarget(
                camera_id=camera_id,
                label=f"camera-{camera_id.hex[:8]}",
                provider_device_id=provider_device,
                provider_component_id=component or None,
            )
        )
    raw_token = getpass.getpass("Ring access token (hidden, memory only): ")
    if not raw_token:
        raise QualificationEnvironmentError("manual credential is required")
    return targets, SecretStr(raw_token)


async def _run_qualification(  # pragma: no cover - hardware/manual Stage 1D-B path
    arguments: argparse.Namespace,
) -> int:
    targets, token = _prompt_manual_targets(arguments.camera_id)
    if arguments.overlap_lead is not None and len(targets) != 1:
        raise QualificationEnvironmentError("overlap experiment requires exactly one camera")
    if arguments.concurrency is not None and len(targets) != arguments.concurrency:
        raise QualificationEnvironmentError("camera count must match explicit concurrency")
    if arguments.overlap_lead is not None and arguments.concurrency is not None:
        raise QualificationEnvironmentError(
            "overlap and multi-camera tests are separate experiments"
        )
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, cancel.set)
        except NotImplementedError:
            pass
    orchestrator = QualificationOrchestrator(
        GStreamerQualificationBackend(), ManualPromptTokenProvider(token)
    )
    config = SequentialConfig(
        mode=arguments.mode,
        session_class=arguments.session_class,
        cycles=arguments.cycles,
        max_retries_per_session=arguments.max_retries,
        decoder_preference=arguments.decoder,
        stall_timeout_seconds=arguments.stall_timeout,
    )
    samples: list[dict[str, object]] = []
    resource_stop = asyncio.Event()
    resource_task = asyncio.create_task(ResourceCollector().monitor(resource_stop, samples))
    try:
        if arguments.overlap_lead is not None:
            first, second, overlap = await orchestrator.run_overlap_experiment(
                targets[0], config, lead_seconds=arguments.overlap_lead, cancel=cancel
            )
            sessions = [first, second]
            summary = continuity_summary(sessions)
            results = [
                CameraQualificationResult(
                    targets[0].camera_id,
                    targets[0].label,
                    config.mode,
                    config.session_class,
                    not cancel.is_set(),
                    sessions,
                    summary,
                    decide(summary, sessions, orchestrator.targets),
                    overlap=overlap,
                )
            ]
        elif arguments.concurrency is not None:
            results = await orchestrator.run_concurrency(
                targets,
                config,
                concurrency=arguments.concurrency,
                ramp_seconds=arguments.ramp_seconds,
                cancel=cancel,
            )
        else:
            if len(targets) != 1:
                raise QualificationEnvironmentError(
                    "sequential test requires exactly one camera or explicit --concurrency"
                )
            results = [await orchestrator.run_sequential(targets[0], config, cancel)]
    finally:
        resource_stop.set()
        await resource_task
    for result in results:
        result.resource_samples = samples
    report = build_report(results, inspect_environment(), incomplete=cancel.is_set())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = arguments.report_dir / f"ring-stream-{stamp}.json"
    write_json_report(report, output)
    print(human_summary(report))
    print(f"Report: {output}")
    return 130 if cancel.is_set() else 0


def main() -> None:  # pragma: no cover - console wrapper verified by smoke command
    arguments = parser().parse_args()
    if arguments.command == "qualify-env":
        print(environment_json())
        return
    if arguments.command == "qualify-transport":
        raise SystemExit(run_transport_cli(arguments))
    if arguments.command == "qualify-webrtc":
        raise SystemExit(run_webrtc_cli(arguments))
    try:
        raise SystemExit(asyncio.run(_run_qualification(arguments)))
    except QualificationEnvironmentError as exc:
        print(f"qualification environment error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
