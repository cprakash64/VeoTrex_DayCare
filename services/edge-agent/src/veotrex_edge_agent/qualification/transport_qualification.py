"""R5A live camera transport qualification. QUALIFICATION ONLY; no media is retained.

Scenarios drive the production transport path (controller + runner + isolated GStreamer worker +
NVIDIA decoder) against the synthetic loopback RTSP fixture. Reports contain timing, counters,
state transitions, and resource metadata only: no frames, URLs, credentials, or provider IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.controller import ControllerConfig, TransportController
from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    CredentialMode,
    ProviderKind,
    SessionCredential,
)
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory
from veotrex_edge_agent.camera_transport.health import StallPolicy
from veotrex_edge_agent.camera_transport.provider import (
    CredentialSource,
    SessionLifetime,
    StaticRtspSessionProvider,
)
from veotrex_edge_agent.camera_transport.reconnect import ReconnectPolicy
from veotrex_edge_agent.camera_transport.runner import CameraTransportRunner
from veotrex_edge_agent.camera_transport.state import TransportState
from veotrex_edge_agent.camera_transport.worker_backend import (
    WorkerBackendConfig,
    WorkerMediaBackend,
)
from veotrex_edge_agent.qualification.metrics import percentile
from veotrex_edge_agent.qualification.models import CameraTarget
from veotrex_edge_agent.qualification.resources import read_tegrastats_once

SYSTEM_PYTHON = Path("/usr/bin/python3")
FIXTURE_SCRIPT = Path(__file__).with_name("rtsp_fixture_server.py")
FIXTURE_CAMERA_ID = UUID("00000000-0000-4000-8000-0000000f1f00")
FIXTURE_USERNAME = "veotrex-fixture"
SCENARIOS = ("smoke", "reconnect", "stall", "renewal", "failures", "soak", "ring-gate")
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


class FixtureServer:
    """Loopback synthetic RTSP fixture process. Password travels over stdin only."""

    def __init__(
        self,
        *,
        codec: str = "h264",
        width: int = 1920,
        height: int = 1080,
        fps: int = 15,
        bitrate_kbps: int = 2000,
        max_session_seconds: float = 0.0,
    ) -> None:
        self._password = "synthetic-" + secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self.settings = {
            "codec": codec,
            "width": width,
            "height": height,
            "fps": fps,
            "bitrate_kbps": bitrate_kbps,
            "max_session_seconds": max_session_seconds,
        }
        self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter and repository script
            [
                str(SYSTEM_PYTHON),
                "-I",
                str(FIXTURE_SCRIPT),
                "--codec",
                codec,
                "--width",
                str(width),
                "--height",
                str(height),
                "--fps",
                str(fps),
                "--bitrate-kbps",
                str(bitrate_kbps),
                "--max-session-seconds",
                str(max_session_seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(self._password + "\n")
        self.process.stdin.flush()
        ready = json.loads(self.process.stdout.readline())
        self.port = int(ready["port"])
        self.canary_port = int(ready["canary_port"])

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def endpoint(self) -> str:
        return f"rtsp://127.0.0.1:{self.port}/stream"

    def credential_source(self, *, wrong: bool = False) -> CredentialSource:
        secret = "synthetic-wrong-" + secrets.token_hex(8) if wrong else self._password

        async def source() -> SessionCredential:
            return SessionCredential(
                CredentialMode.RTSP_USER_PASSWORD, FIXTURE_USERNAME, SecretStr(secret)
            )

        return source

    def secret_bytes(self) -> bytes:
        return self._password.encode()

    def command(self, text: str) -> dict[str, Any]:
        with self._lock:
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.write(text + "\n")
            self.process.stdin.flush()
            value = json.loads(self.process.stdout.readline())
        assert isinstance(value, dict)
        return value

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError, AssertionError):
            assert self.process.stdin is not None
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def __enter__(self) -> FixtureServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ------------------------------------------------------------------------- resource probes
def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def process_rss_bytes(pid: int) -> int | None:
    status = _read(f"/proc/{pid}/status")
    for line in (status or "").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def process_threads(pid: int) -> int | None:
    status = _read(f"/proc/{pid}/status")
    for line in (status or "").splitlines():
        if line.startswith("Threads:"):
            return int(line.split()[1])
    return None


def fd_count(pid: int) -> int | None:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None


def _stat_fields(pid: int) -> list[str] | None:
    stat = _read(f"/proc/{pid}/stat")
    return stat.rsplit(")", 1)[1].split() if stat and ")" in stat else None


def process_state(pid: int) -> str | None:
    fields = _stat_fields(pid)
    return fields[0] if fields else None


def cpu_ticks(pid: int) -> int | None:
    fields = _stat_fields(pid)
    return int(fields[11]) + int(fields[12]) if fields else None


def child_pids(pid: int) -> list[int]:
    children: list[int] = []
    with contextlib.suppress(OSError):
        for task in os.listdir(f"/proc/{pid}/task"):
            raw = _read(f"/proc/{pid}/task/{task}/children") or ""
            children.extend(int(value) for value in raw.split())
    return sorted(set(children))


def _system_cpu() -> tuple[int, int] | None:
    first = (_read("/proc/stat") or "").splitlines()[:1]
    if not first:
        return None
    values = [int(value) for value in first[0].split()[1:]]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def _mem_available() -> int | None:
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


class ResourceProbe:
    def __init__(self) -> None:
        self._cpu: dict[int, tuple[int, float]] = {}
        self._system: tuple[int, int] | None = None

    def _cpu_percent(self, pid: int, now: float) -> float | None:
        ticks = cpu_ticks(pid)
        if ticks is None:
            return None
        previous = self._cpu.get(pid)
        self._cpu[pid] = (ticks, now)
        if previous is None or now <= previous[1]:
            return None
        return (ticks - previous[0]) / _CLOCK_TICKS / (now - previous[1]) * 100

    def sample(self, worker_pids: list[int], excluded: set[int], offset: float) -> dict[str, Any]:
        now = time.monotonic()
        me = os.getpid()
        children = [pid for pid in child_pids(me) if pid not in excluded]
        system = _system_cpu()
        system_percent = None
        if system and self._system and system[0] > self._system[0]:
            total, idle = system[0] - self._system[0], system[1] - self._system[1]
            system_percent = (total - idle) / total * 100
        self._system = system
        return {
            "offset_seconds": round(offset, 3),
            "edge_agent_rss_bytes": process_rss_bytes(me),
            "edge_agent_fds": fd_count(me),
            "edge_agent_threads": process_threads(me),
            "edge_agent_cpu_percent": self._cpu_percent(me, now),
            "workers": [
                {
                    "rss_bytes": process_rss_bytes(pid),
                    "fds": fd_count(pid),
                    "threads": process_threads(pid),
                    "cpu_percent": self._cpu_percent(pid, now),
                }
                for pid in worker_pids
            ],
            "child_processes": len(children),
            "zombie_children": sum(1 for pid in children if process_state(pid) == "Z"),
            "system_cpu_percent": system_percent,
            "memory_available_bytes": _mem_available(),
            "load_1m": os.getloadavg()[0],
            "nvdec_clock_hz": _nvdec_clock_hz(),
            "tegrastats": read_tegrastats_once(),
        }


def _nvdec_clock_hz() -> int | None:
    """NVDEC devfreq clock (readable without root). Utilization needs debugfs and is not read."""
    for path in sorted(Path("/sys/class/devfreq").glob("*.nvdec")):
        raw = _read(str(path / "cur_freq"))
        if raw and raw.strip().isdigit():
            return int(raw.strip())
    return None


def summarize_resources(samples: list[dict[str, Any]]) -> dict[str, Any]:
    def series(extract: Callable[[dict[str, Any]], Any]) -> list[float]:
        values = []
        for sample in samples:
            with contextlib.suppress(KeyError, IndexError, TypeError):
                value = extract(sample)
                if isinstance(value, int | float):
                    values.append(float(value))
        return values

    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"first": None, "last": None, "min": None, "max": None, "p50": None}
        return {
            "first": values[0],
            "last": values[-1],
            "min": min(values),
            "max": max(values),
            "p50": percentile(values, 50),
        }

    def tegra_number(key: str) -> Callable[[dict[str, Any]], Any]:
        def extract(sample: dict[str, Any]) -> Any:
            raw = str(sample["tegrastats"].get(key, ""))
            head = raw.split("@", 1)[0].split("/", 1)[0].rstrip("%C")
            return float(head) if head.replace(".", "", 1).isdigit() else None

        return extract

    return {
        "samples": len(samples),
        "edge_agent_rss_bytes": stats(series(lambda s: s["edge_agent_rss_bytes"])),
        "edge_agent_fds": stats(series(lambda s: s["edge_agent_fds"])),
        "edge_agent_cpu_percent": stats(series(lambda s: s["edge_agent_cpu_percent"])),
        "worker_rss_bytes": stats(series(lambda s: s["workers"][0]["rss_bytes"])),
        "worker_fds": stats(series(lambda s: s["workers"][0]["fds"])),
        "worker_threads": stats(series(lambda s: s["workers"][0]["threads"])),
        "worker_cpu_percent": stats(series(lambda s: s["workers"][0]["cpu_percent"])),
        "max_concurrent_workers": max((len(s["workers"]) for s in samples), default=0),
        "child_processes": stats(series(lambda s: s["child_processes"])),
        "zombie_children_max": max((s["zombie_children"] for s in samples), default=0),
        "system_cpu_percent": stats(series(lambda s: s["system_cpu_percent"])),
        "memory_available_bytes": stats(series(lambda s: s["memory_available_bytes"])),
        "gpu_gr3d_percent": stats(series(tegra_number("gpu"))),
        "nvdec": sorted({str(s["tegrastats"].get("nvdec")) for s in samples}),
        "temperature_cpu_c": stats(series(tegra_number("temperature_cpu"))),
        "temperature_gpu_c": stats(series(tegra_number("temperature_gpu"))),
        "temperature_tj_c": stats(series(tegra_number("temperature_tj"))),
        "throttling_observed": any(s["tegrastats"].get("throttling_observed") for s in samples),
    }


# ------------------------------------------------------------------------- scenario runner
@dataclass(slots=True)
class ScenarioPlan:
    name: str
    duration_seconds: float
    events: list[tuple[float, str]] = field(default_factory=list)
    lifetime: SessionLifetime | None = None
    wrong_credential: bool = False
    endpoint: str | None = None
    config: ControllerConfig = field(default_factory=ControllerConfig)
    backend: WorkerBackendConfig = field(default_factory=WorkerBackendConfig)
    stop_on_failed: bool = False
    sample_interval_seconds: float = 5.0
    fixture_commands_before: tuple[str, ...] = ()
    fixture_commands_after: tuple[str, ...] = ()


async def run_plan(plan: ScenarioPlan, fixture: FixtureServer) -> dict[str, Any]:
    for text in plan.fixture_commands_before:
        await asyncio.to_thread(fixture.command, text)
    provider = StaticRtspSessionProvider(
        ProviderKind.LOCAL_FIXTURE,
        plan.endpoint or fixture.endpoint,
        LOCAL_FIXTURE_ENDPOINT_POLICY,
        credential_source=fixture.credential_source(wrong=plan.wrong_credential),
        lifetime=plan.lifetime,
    )
    controller = TransportController(FIXTURE_CAMERA_ID, ProviderKind.LOCAL_FIXTURE, plan.config)
    me = os.getpid()
    excluded = {fixture.pid}
    baseline_fds = fd_count(me)
    baseline_children = [pid for pid in child_pids(me) if pid not in excluded]
    probe = ResourceProbe()
    samples: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    kills: list[dict[str, Any]] = []
    secret_exposures: list[str] = []
    stop = asyncio.Event()
    start = time.monotonic()

    def observer(runner: CameraTransportRunner, now: float) -> None:
        for kill in kills:
            if (
                kill.get("recovery_seconds") is None
                and controller.state is TransportState.STREAMING
                and (controller.current_generation or 0) > kill["generation"]
            ):
                kill["recovery_seconds"] = round(now - kill["_at"], 3)
                kill["new_generation"] = controller.current_generation
        if plan.stop_on_failed and controller.state is TransportState.FAILED:
            stop.set()

    runner = CameraTransportRunner(
        controller,
        provider,
        lambda generation: WorkerMediaBackend(generation, plan.backend),
        observer=observer,
    )

    async def perform(action: str) -> dict[str, Any]:
        offset = round(time.monotonic() - start, 3)
        if action == "kill":
            generation = controller.current_generation
            handle = runner.handles.get(generation) if generation is not None else None
            pid = handle.pid if handle is not None else None
            if pid is None:
                return {"offset": offset, "action": action, "result": "no_worker"}
            os.kill(pid, signal.SIGKILL)  # VeoTrex's own worker only; camera/network untouched.
            kills.append(
                {
                    "offset": offset,
                    "_at": time.monotonic(),
                    "generation": generation,
                    "state_before": controller.state.value,
                    "recovery_seconds": None,
                    "_pid": pid,
                }
            )
            return {"offset": offset, "action": action, "generation": generation}
        response = await asyncio.to_thread(fixture.command, action)
        return {"offset": offset, "action": action, "ack": response.get("event")}

    async def script() -> None:
        for offset, action in sorted(plan.events):
            delay = start + offset - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
            notes.append(await perform(action))

    async def sampler() -> None:
        while not stop.is_set():
            pids = [pid for pid in (h.pid for h in runner.handles.values()) if pid is not None]
            for pid in pids:
                with contextlib.suppress(OSError):
                    exposed = fixture.secret_bytes()
                    if (
                        exposed in Path(f"/proc/{pid}/cmdline").read_bytes()
                        or exposed in Path(f"/proc/{pid}/environ").read_bytes()
                    ):
                        secret_exposures.append("worker_argv_or_environment")
            samples.append(
                await asyncio.to_thread(probe.sample, pids, excluded, time.monotonic() - start)
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=plan.sample_interval_seconds)

    run_task = asyncio.create_task(runner.run(stop))
    helpers = [asyncio.create_task(script()), asyncio.create_task(sampler())]
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=plan.duration_seconds)
    # A self-terminating scenario may already have been shut down by the runner; report the state
    # it was in before the final transition to STOPPED.
    history = controller.machine.history
    stopped = controller.state is TransportState.STOPPED and history
    state_before_stop = history[-1].previous.value if stopped else controller.state.value
    health_before_stop = controller.health(time.monotonic()).as_dict()
    stop.set()
    await run_task
    for task in helpers:
        task.cancel()
    await asyncio.gather(*helpers, return_exceptions=True)
    await asyncio.sleep(1.0)
    for text in plan.fixture_commands_after:
        await asyncio.to_thread(fixture.command, text)
    children_after = [pid for pid in child_pids(me) if pid not in excluded]
    retired = runner.retired_handles
    worker_rows = [
        {
            "generation": getattr(handle, "generation", None),
            "returncode": getattr(handle, "returncode", None),
            "stopped_cleanly": getattr(handle, "stopped_cleanly", None),
            "protocol_errors": getattr(handle, "protocol_errors", None),
            "worker_dropped_samples": getattr(handle, "worker_dropped_samples", None),
        }
        for handle in retired
    ]
    killed_pids = {kill.pop("_pid") for kill in kills}
    for kill in kills:
        kill.pop("_at", None)
    hello = next((getattr(h, "hello", None) for h in retired if getattr(h, "hello", None)), None)
    records = [
        {key: value for key, value in record.items() if key != "ended_at"}
        | {"ended_offset": round(record["ended_at"] - start, 3)}
        for record in controller.session_records
    ]
    timeline = controller.timeline.summary()
    decoded_duration = sum(r["media_duration_seconds"] or 0.0 for r in records)
    decoded = sum(r["decoded_buffers"] for r in records)
    return {
        "scenario": plan.name,
        "configured_duration_seconds": plan.duration_seconds,
        "observed_duration_seconds": round(time.monotonic() - start, 3),
        "state_before_stop": state_before_stop,
        "health_before_stop": {
            key: value
            for key, value in health_before_stop.items()
            if key not in {"last_media_monotonic"}
        },
        "final_state": controller.state.value,
        "transitions": [
            {
                "offset": round(record.at - start, 3),
                "previous": record.previous.value,
                "state": record.state.value,
                "category": record.category,
                "generation": record.generation,
            }
            for record in controller.machine.history
        ],
        "sessions": records,
        "timeline": timeline,
        "observed_decoded_fps": round(decoded / decoded_duration, 3) if decoded_duration else None,
        "handoff_gaps_ms": [round(v * 1000, 3) for v in controller.handoff_gaps.values()],
        "metrics": controller.metrics.snapshot(),
        "circuit_open": controller.circuit_open,
        "last_failure": controller.last_failure.value if controller.last_failure else None,
        "last_renewal_failure": (
            controller.last_renewal_failure.value if controller.last_renewal_failure else None
        ),
        "expiry_reconnects": controller.expiry_reconnects,
        "controlled_kills": kills,
        "events": notes,
        "worker_hello": hello,
        "workers": worker_rows,
        "resources": summarize_resources(samples),
        "resource_samples": samples,
        "post_stop": {
            "edge_agent_fds_before": baseline_fds,
            "edge_agent_fds_after": fd_count(me),
            "children_before": len(baseline_children),
            "children_after": len(children_after),
            "zombies_after": sum(1 for pid in children_after if process_state(pid) == "Z"),
            "killed_workers_reaped": all(
                process_state(pid) in {None} or pid not in child_pids(me) for pid in killed_pids
            ),
            "active_handles": len(runner.handles),
        },
        "secret_exposures": secret_exposures,
        "fixture": await asyncio.to_thread(fixture.command, "stats"),
    }


def _fast_reconnect(**overrides: Any) -> ReconnectPolicy:
    values: dict[str, Any] = {"initial_delay_seconds": 1.0, "maximum_delay_seconds": 8.0}
    values.update(overrides)
    return ReconnectPolicy(**values)


def evaluate(result: dict[str, Any], expectations: dict[str, Any]) -> dict[str, bool]:
    post = result["post_stop"]
    checks = {
        "no_zombies": post["zombies_after"] == 0
        and result["resources"]["zombie_children_max"] == 0,
        "no_fd_leak": post["edge_agent_fds_after"] == post["edge_agent_fds_before"],
        "no_child_leak": post["children_after"] == post["children_before"],
        "no_active_handles": post["active_handles"] == 0,
        "no_secret_exposure": not result["secret_exposures"],
    }
    for key, expected in expectations.items():
        if key == "state_before_stop":
            checks[key] = result["state_before_stop"] == expected
        elif key == "last_failure":
            checks[key] = result["last_failure"] == expected
        elif key == "hardware_decode":
            checks[key] = all(
                s["hardware_decoder"] and s["nvmm"]
                for s in result["sessions"]
                if s["decoded_buffers"]
            ) and any(s["decoded_buffers"] for s in result["sessions"])
        elif key == "min_renewals":
            checks[key] = (
                result["metrics"]["counters"]["camera_transport_renewals_total"] >= expected
            )
        elif key == "max_renewal_failures":
            checks[key] = (
                result["metrics"]["counters"]["camera_transport_renewal_failures_total"] <= expected
            )
        elif key == "kills_recovered":
            recovered = [k for k in result["controlled_kills"] if k["recovery_seconds"] is not None]
            checks[key] = len(recovered) == expected and all(
                k["recovery_seconds"] < 20 for k in recovered
            )
        elif key == "max_reconnects":
            checks[key] = (
                result["metrics"]["counters"]["camera_transport_reconnects_total"] <= expected
            )
        elif key == "min_reconnects":
            checks[key] = (
                result["metrics"]["counters"]["camera_transport_reconnects_total"] >= expected
            )
        elif key == "min_stalls":
            checks[key] = result["metrics"]["counters"]["camera_transport_stalls_total"] >= expected
        elif key == "no_timestamp_regressions":
            timeline = result["timeline"]
            checks[key] = (
                timeline["decoded_pts_regressions"] == 0 and timeline["large_reversals"] == 0
            )
        elif key == "circuit_open":
            checks[key] = result["circuit_open"] is expected
        elif key == "canary_auth_headers":
            checks[key] = result["fixture"]["canary_auth_headers"] == expected
        elif key == "max_rss_growth_bytes":
            rss = result["resources"]["worker_rss_bytes"]
            checks[key] = (
                rss["first"] is not None
                and rss["max"] is not None
                and rss["max"] - rss["first"] <= expected
            )
    return checks


def plans_for(scenario: str, duration: float | None) -> list[tuple[ScenarioPlan, dict[str, Any]]]:
    common: dict[str, Any] = {"hardware_decode": True}
    if scenario == "smoke":
        return [
            (
                ScenarioPlan("smoke", duration or 60.0),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "max_reconnects": 0,
                    "no_timestamp_regressions": True,
                },
            )
        ]
    if scenario == "reconnect":
        return [
            (
                ScenarioPlan(
                    "controlled_reconnect",
                    duration or 90.0,
                    events=[(20.0, "kill"), (45.0, "kill"), (70.0, "kill")],
                    config=ControllerConfig(reconnect=_fast_reconnect()),
                ),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "kills_recovered": 3,
                    "max_reconnects": 3,
                },
            )
        ]
    if scenario == "stall":
        return [
            (
                ScenarioPlan(
                    "stall",
                    duration or 80.0,
                    events=[(15.0, "stall 4"), (40.0, "stall 14")],
                    config=ControllerConfig(reconnect=_fast_reconnect()),
                ),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "min_stalls": 2,
                    "min_reconnects": 1,
                    "max_reconnects": 2,
                },
            )
        ]
    if scenario == "renewal":
        return [
            (
                ScenarioPlan(
                    "overlap_renewal",
                    duration or 160.0,
                    lifetime=SessionLifetime(30.0, 5.0),
                    fixture_commands_before=("expire 30",),
                    fixture_commands_after=("expire 0",),
                ),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "min_renewals": 4,
                    "max_renewal_failures": 0,
                    "max_reconnects": 0,
                },
            )
        ]
    if scenario == "failures":
        closed = _closed_loopback_port()
        circuit = ControllerConfig(
            reconnect=ReconnectPolicy(
                initial_delay_seconds=0.5, maximum_delay_seconds=2.0, max_attempts=3
            ),
            stall=StallPolicy(first_media_timeout_seconds=10.0),
        )
        return [
            (
                ScenarioPlan(
                    "authorization_failure",
                    30.0,
                    wrong_credential=True,
                    stop_on_failed=True,
                    sample_interval_seconds=1.0,
                ),
                {
                    "state_before_stop": "FAILED",
                    "last_failure": "AUTHORIZATION_FAILED",
                    "max_reconnects": 0,
                },
            ),
            (
                ScenarioPlan(
                    "connect_refused_circuit",
                    90.0,
                    endpoint=f"rtsp://127.0.0.1:{closed}/stream",
                    config=circuit,
                    stop_on_failed=True,
                    sample_interval_seconds=1.0,
                ),
                {
                    "state_before_stop": "FAILED",
                    "last_failure": "TRANSPORT_CONNECT_FAILED",
                    "circuit_open": True,
                    "max_reconnects": 3,
                },
            ),
            (
                ScenarioPlan(
                    "redirect_refused",
                    30.0,
                    stop_on_failed=True,
                    fixture_commands_before=("redirect on",),
                    fixture_commands_after=("redirect off",),
                    sample_interval_seconds=1.0,
                ),
                {
                    "state_before_stop": "FAILED",
                    "last_failure": "REDIRECT_REFUSED",
                    "canary_auth_headers": 0,
                },
            ),
            (
                ScenarioPlan(
                    "cross_origin_control_refused",
                    30.0,
                    stop_on_failed=True,
                    fixture_commands_before=("cross-origin-control on",),
                    fixture_commands_after=("cross-origin-control off",),
                    sample_interval_seconds=1.0,
                ),
                {
                    "state_before_stop": "FAILED",
                    "last_failure": "REDIRECT_REFUSED",
                    "canary_auth_headers": 0,
                },
            ),
            (
                ScenarioPlan(
                    "provider_drop_reconnect",
                    40.0,
                    events=[(15.0, "drop")],
                    config=ControllerConfig(reconnect=_fast_reconnect()),
                    sample_interval_seconds=2.0,
                ),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "min_reconnects": 1,
                    "max_reconnects": 1,
                },
            ),
        ]
    if scenario == "soak":
        return [
            (
                ScenarioPlan("transport_soak", duration or 1800.0, sample_interval_seconds=10.0),
                {
                    **common,
                    "state_before_stop": "STREAMING",
                    "max_reconnects": 0,
                    "no_timestamp_regressions": True,
                    "max_rss_growth_bytes": 32 * 1024 * 1024,
                },
            )
        ]
    raise ValueError("unknown scenario")


def _closed_loopback_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ------------------------------------------------------------------------- Ring gate
RING_CAPABILITY_MATRIX: dict[str, dict[str, str]] = {
    "A_inventory": {
        "officially_exposed": "yes (GET /v1/devices, capabilities, configurations)",
        "project_implementation": "implemented (apps/api ring_client.discover_devices)",
        "status": "NOT_AUTHORIZED",
    },
    "B_snapshot": {
        "officially_exposed": "yes (POST /v1/devices/{id}/media/image/download)",
        "project_implementation": "not implemented (CameraProvider.request_snapshot placeholder)",
        "status": "NOT_IMPLEMENTED",
    },
    "C_live_session_creation": {
        "officially_exposed": "WHEP: POST .../media/streaming/whep/sessions; RTSPS: URL + token",
        "project_implementation": "RTSPS descriptor via RingLiveSessionProvider; WHEP absent",
        "status": "NOT_AUTHORIZED",
    },
    "D_live_media_transport": {
        "officially_exposed": "RTSPS rtsps://video.rtsp.amazonvision.com:322/...; WebRTC/WHEP",
        "project_implementation": "RTSPS path implemented and qualified on loopback only; "
        "WHEP/WebRTC not implemented (whepsrc absent)",
        "status": "NOT_AUTHORIZED",
    },
    "E_session_renewal": {
        "officially_exposed": "current public page does not state RTSPS session limits",
        "project_implementation": "overlap renewal implemented and fixture-qualified",
        "status": "UNKNOWN",
    },
    "F_other_webhooks_clips": {
        "officially_exposed": "webhooks, media clips",
        "project_implementation": "webhooks implemented (needs DB + partner HMAC key); "
        "clips not implemented",
        "status": "NOT_AUTHORIZED",
    },
}


async def ring_gate() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[5]
    env_names = sorted(name for name in os.environ if "RING" in name.upper())
    env_files = [
        str(path.relative_to(root))
        for path in (root / ".env", root / "apps/api/.env", root / "services/edge-agent/.env")
        if path.exists()
    ]
    target = CameraTarget(
        camera_id=UUID("00000000-0000-4000-8000-0000000a1a90"),
        label="ring-gate-probe",
        provider_device_id="synthetic-probe-device",
    )
    from veotrex_edge_agent.camera_transport.webrtc_media import probe_webrtc_runtime
    from veotrex_edge_agent.camera_transport.whep_provider import RingWhepSessionProvider

    # R5A-R1: the official live-video path is WHEP; the legacy RTSPS provider is unverified and
    # cannot be constructed without an explicit acknowledgement, so it is not probed here.
    runtime = probe_webrtc_runtime()
    provider = RingWhepSessionProvider(target, None)
    try:
        await provider.acquire(target.camera_id, 1, time.monotonic())
        probe = "UNEXPECTED_SUCCESS"
    except TransportError as exc:
        probe = exc.category.value
    configured = bool(env_names or env_files)
    return {
        "scenario": "ring-gate",
        "ring_environment_variable_names_present": env_names,
        "env_files_present": env_files,
        "credential_vault": "UnavailableCredentialVault (production fails closed; no managed "
        "adapter configured)",
        "edge_token_provider_injected": False,
        "ring_provider_acquire_probe": probe,
        "account_or_partner_credentials_configured": configured,
        "capabilities": RING_CAPABILITY_MATRIX,
        "sub_gate": "BLOCKED_RING_ACCOUNT_NOT_AVAILABLE" if not configured else "CONFIGURED",
        "ring_live_media_decision": "NOT_CONFIGURED" if not configured else "UNVERIFIED",
        "expected_probe": TransportErrorCategory.PROVIDER_NOT_CONFIGURED.value,
        "evidence_date": "2026-09-11",
        "portal_readiness_report": "reports/qualification/r5a_r1_ring_portal_setup.md",
        "whep": {
            "documented_endpoint": "POST /v1/devices/{device_id}/media/streaming/whep/sessions",
            "control_plane_implemented": True,
            "media_plane_available": runtime.available,
            "webrtc_runtime": runtime.as_dict(),
            "decision": "QUALIFIED_SYNTHETIC"
            if not runtime.available
            else "READY_FOR_REAL_CREDENTIALS",
        },
        "legacy_rtsps": "LEGACY_UNVERIFIED (not probed; requires explicit acknowledgement)",
    }


# ------------------------------------------------------------------------- CLI entry
def _write(report: dict[str, Any], directory: Path, scenario: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"r5a-transport-{scenario}-{stamp}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), "utf-8")
    os.replace(temporary, path)
    return path


async def run_scenario(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.scenario == "ring-gate":
        return await ring_gate()
    if os.environ.get("GST_DEBUG") not in {None, "", "0"}:
        raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
    results = []
    with FixtureServer(
        codec=arguments.codec,
        width=arguments.width,
        height=arguments.height,
        fps=arguments.fps,
    ) as fixture:
        for plan, expectations in plans_for(arguments.scenario, arguments.duration):
            result = await run_plan(plan, fixture)
            result["checks"] = evaluate(result, expectations)
            result["passed"] = all(result["checks"].values())
            results.append(result)
        settings = fixture.settings
    return {
        "stage": "R5A",
        "scenario": arguments.scenario,
        "fixture": {**settings, "content": "synthetic videotestsrc; loopback only"},
        "media_persistence": "none (fakesink; timing metadata only)",
        "passed": all(result["passed"] for result in results),
        "results": results,
    }


def run_transport_cli(arguments: argparse.Namespace) -> int:  # pragma: no cover - hardware path
    report = asyncio.run(run_scenario(arguments))
    path = _write(report, arguments.report_dir, arguments.scenario)
    summary: dict[str, Any] = {"report": str(path), "passed": report.get("passed")}
    for result in report.get("results", []):
        summary[result["scenario"]] = {
            "passed": result["passed"],
            "failed_checks": [key for key, ok in result["checks"].items() if not ok],
            "state_before_stop": result["state_before_stop"],
            "decoded": result["timeline"]["decoded_buffers"],
            "fps": result["observed_decoded_fps"],
        }
    if arguments.scenario == "ring-gate":
        summary.update({k: report[k] for k in ("sub_gate", "ring_live_media_decision")})
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report.get("passed", True) else 1
