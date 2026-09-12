"""R5A-R2 local WebRTC media qualification. QUALIFICATION ONLY; no media is retained.

Drives the production WebRTC receive path (isolated `webrtc_worker.py` + `nvv4l2decoder`) against
a synthetic local sending peer over loopback/host ICE candidates. No STUN/TURN, no signaling
server, no Internet dependency, and no Ring credential: the WHEP scenario uses the R5A-R1 synthetic
endpoint with an obviously synthetic Bearer token. Reports contain timing, counters and resource
metadata only: never SDP bodies, ICE candidates, frames, or credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendExited,
    BackendFailed,
    DecodedCaps,
    EndOfStream,
    MediaBatch,
    MediaNegotiated,
    TransportConnected,
)
from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    NO_CREDENTIAL,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    VideoCodec,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.webrtc_backend import (
    WebRtcBackendConfig,
    WebRtcMediaBackend,
)
from veotrex_edge_agent.camera_transport.webrtc_media import probe_webrtc_runtime
from veotrex_edge_agent.camera_transport.whep_client import WhepClient, WhepConfig
from veotrex_edge_agent.qualification.metrics import BoundedSamples
from veotrex_edge_agent.qualification.transport_qualification import (
    ResourceProbe,
    child_pids,
    fd_count,
    process_state,
    summarize_resources,
)
from veotrex_edge_agent.qualification.whep_fixture_server import (
    SYNTHETIC_BEARER,
    WhepFixtureServer,
    WhepScript,
)

SYSTEM_PYTHON = Path("/usr/bin/python3")
PEER_SCRIPT = Path(__file__).with_name("webrtc_fixture_peer.py")
LOCAL_CAMERA_ID = UUID("00000000-0000-4000-8000-000000007e60")
SCENARIOS = ("local", "whep", "negatives", "cycles", "recovery", "soak")
SYNTHETIC_DEVICE = "synthetic-local-device"


def local_lease(generation: int) -> LiveSessionLease:
    """Descriptor for a purely local synthetic session; no provider endpoint is contacted."""
    return LiveSessionLease(
        LiveSessionDescriptor(
            provider=ProviderKind.LOCAL_FIXTURE,
            logical_camera_id=LOCAL_CAMERA_ID,
            generation=generation,
            endpoint=validate_endpoint(
                "rtsp://127.0.0.1:9/synthetic-webrtc", LOCAL_FIXTURE_ENDPOINT_POLICY
            ),
            created_monotonic=time.monotonic(),
        ),
        NO_CREDENTIAL,
    )


class LocalSendingPeer:
    """Synthetic WebRTC sender subprocess with deterministic stdin/stdout signaling."""

    def __init__(
        self,
        *,
        codec: str = "H264",
        width: int = 1280,
        height: int = 720,
        fps: int = 15,
        mode: str = "normal",
    ) -> None:
        self.settings = {
            "codec": codec,
            "width": width,
            "height": height,
            "fps": fps,
            "mode": mode,
        }
        self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter and repository script
            [
                str(SYSTEM_PYTHON),
                "-I",
                str(PEER_SCRIPT),
                "--codec",
                codec,
                "--width",
                str(width),
                "--height",
                str(height),
                "--fps",
                str(fps),
                "--mode",
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        self.ready = json.loads(self.process.stdout.readline())
        self.answer_seconds: float | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    def _command(self, value: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def answer_for(self, offer_sdp: str, _lease: LiveSessionLease) -> str:
        """AnswerExchange for the purely local path (no WHEP control plane involved)."""
        started = time.monotonic()
        self._command({"type": "OFFER", "sdp": offer_sdp})
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise TransportError.__new__(TransportError)  # peer vanished mid-negotiation
            message = json.loads(line)
            if message.get("event") == "answer":
                self.answer_seconds = time.monotonic() - started
                answer = message["sdp"]
                assert isinstance(answer, str)
                return answer
            if message.get("event") == "error":
                raise RuntimeError(str(message.get("detail"))[:64])

    def stall(self, seconds: float) -> None:
        self._command({"type": "STALL", "seconds": seconds})
        assert self.process.stdout is not None
        self.process.stdout.readline()

    def stats(self) -> dict[str, Any]:
        self._command({"type": "STATS"})
        assert self.process.stdout is not None
        value = json.loads(self.process.stdout.readline())
        assert isinstance(value, dict)
        return value

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            os.kill(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError, AssertionError, BrokenPipeError):
            self._command({"type": "STOP"})
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        # Popen holds the stdin/stdout pipe descriptors until it is garbage collected; close them
        # explicitly so descriptor counts return to the pre-session baseline.
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    def __enter__(self) -> LocalSendingPeer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WhepBridge:
    """Routes the worker's offer through the real WHEP client to the synthetic endpoint.

    The endpoint answers with the local peer's SDP, so the production control-plane validation
    (Bearer header placement, status handling, SDP answer checks, Location validation, teardown)
    is exercised on the way to real media.
    """

    def __init__(self, peer: LocalSendingPeer, server: WhepFixtureServer) -> None:
        self.peer = peer
        self.server = server
        self.client = WhepClient(
            WhepConfig(host="api.amazonvision.com", port=443),
            connection_factory=server.connection_factory(),
        )
        self.session_url: str | None = None
        self.control_seconds: float | None = None

    def answer_for(self, offer_sdp: str, lease: LiveSessionLease) -> str:
        answer = self.peer.answer_for(offer_sdp, lease)
        location = (
            f"https://api.amazonvision.com:443/v1/devices/{SYNTHETIC_DEVICE}"
            "/media/streaming/whep/sessions/synthetic-session-1"
        )
        self.server.script(WhepScript(status=201, body=answer.encode(), location=location))
        started = time.monotonic()
        session = self.client.create_session(
            SYNTHETIC_DEVICE, SecretStr(SYNTHETIC_BEARER), offer_sdp
        )
        self.control_seconds = time.monotonic() - started
        self.session_url = session.session_url
        return session.answer_sdp

    def teardown(self) -> bool:
        if self.session_url is None:
            return False
        self.server.script(WhepScript(status=204, body=b"", content_type=None))
        self.client.delete_session(self.session_url, SecretStr(SYNTHETIC_BEARER))
        return True


@dataclass
class SessionOutcome:
    events: list[BackendEvent] = field(default_factory=list)
    backend: WebRtcMediaBackend | None = None
    failure: str | None = None
    started_at: float = 0.0
    first_decoded_at: float | None = None


class MediaAggregate:
    """Folds backend events into bounded counters.

    A 15-minute session produces ~110k media samples; retaining the event objects made the
    qualification process itself grow without bound, so events are aggregated as they arrive and
    only bounded gap samples are kept.
    """

    def __init__(self, gap_capacity: int = 65_536) -> None:
        self.rtp_buffers = 0
        self.decoded_buffers = 0
        self.first_decoded_arrival: float | None = None
        self.last_decoded_arrival: float | None = None
        self.last_decoded_pts: int | None = None
        self.timestamp_regressions = 0
        self.stalls_over_2s = 0
        self.gaps = BoundedSamples(gap_capacity)
        self.connected = False
        self.end_of_stream = False
        self.exited = False
        self.negotiated: MediaNegotiated | None = None
        self.caps: DecodedCaps | None = None
        self.failures: list[str] = []

    def feed(self, event: BackendEvent) -> None:
        if isinstance(event, MediaBatch):
            self.rtp_buffers += len(event.compressed)
            for sample in event.decoded:
                self.decoded_buffers += 1
                if self.first_decoded_arrival is None:
                    self.first_decoded_arrival = sample.arrival
                previous = self.last_decoded_arrival
                if previous is not None and sample.arrival >= previous:
                    gap = sample.arrival - previous
                    self.gaps.add(gap)
                    if gap > 2.0:
                        self.stalls_over_2s += 1
                self.last_decoded_arrival = sample.arrival
                if sample.pts_ns is not None:
                    if self.last_decoded_pts is not None and sample.pts_ns < self.last_decoded_pts:
                        self.timestamp_regressions += 1
                    self.last_decoded_pts = sample.pts_ns
        elif isinstance(event, TransportConnected):
            self.connected = True
        elif isinstance(event, MediaNegotiated):
            self.negotiated = self.negotiated or event
        elif isinstance(event, DecodedCaps):
            self.caps = self.caps or event
        elif isinstance(event, BackendFailed):
            self.failures.append(event.category.value)
        elif isinstance(event, EndOfStream):
            self.end_of_stream = True
        elif isinstance(event, BackendExited):
            self.exited = True

    def summary(self) -> dict[str, Any]:
        first, last = self.first_decoded_arrival, self.last_decoded_arrival
        duration = (last - first) if first is not None and last is not None else 0.0
        negotiated, caps = self.negotiated, self.caps

        def ms(value: float | None) -> float | None:
            return None if value is None else round(value * 1000, 3)

        return {
            "rtp_buffers": self.rtp_buffers,
            "decoded_buffers": self.decoded_buffers,
            # A target-bounded short session only captures the negotiation/jitter-buffer startup
            # burst, so a rate computed over it is real but unrepresentative; report it only over
            # a window long enough to mean steady-state decode.
            "decoded_window_seconds": round(duration, 3),
            "decoded_fps": (
                round((self.decoded_buffers - 1) / duration, 3) if duration >= 2.0 else None
            ),
            "p50_gap_ms": ms(self.gaps.percentile(50)),
            "p95_gap_ms": ms(self.gaps.percentile(95)),
            "p99_gap_ms": ms(self.gaps.percentile(99)),
            "max_gap_ms": ms(self.gaps.maximum),
            "gap_samples_retained": self.gaps.retained_count,
            "timestamp_regressions": self.timestamp_regressions,
            "stalls_over_2s": self.stalls_over_2s,
            "connected": self.connected,
            "codec": negotiated.codec.value if negotiated else None,
            "decoder": negotiated.decoder if negotiated else None,
            "hardware_decoder": negotiated.hardware_decoder if negotiated else None,
            "width": caps.width if caps else None,
            "height": caps.height if caps else None,
            "nvmm": caps.nvmm if caps else None,
            "failures": list(self.failures),
            "end_of_stream": self.end_of_stream,
        }


def run_session(
    peer: LocalSendingPeer,
    exchange: Any,
    generation: int,
    *,
    duration_seconds: float,
    target_decoded: int | None = None,
    config: WebRtcBackendConfig | None = None,
    sampler: ResourceProbe | None = None,
    samples: list[dict[str, Any]] | None = None,
    sample_interval_seconds: float = 10.0,
    on_tick: Any = None,
) -> dict[str, Any]:
    aggregate = MediaAggregate()
    backend = WebRtcMediaBackend(generation, exchange, config or WebRtcBackendConfig())
    started = time.monotonic()
    result: dict[str, Any] = {"generation": generation}
    try:
        backend.start(local_lease(generation), aggregate.feed)
    except TransportError as exc:
        result["start_failure"] = exc.category.value
        result["start_seconds"] = round(time.monotonic() - started, 3)
        with contextlib.suppress(Exception):
            backend.stop()
        return result
    result["start_seconds"] = round(time.monotonic() - started, 3)
    result["offer_candidates"] = backend.offer_candidates
    result["offer_seconds"] = round((backend.offer_at or started) - started, 3)
    result["answer_seconds"] = round((backend.answer_at or started) - started, 3)
    deadline = started + duration_seconds
    next_sample = started
    first_decoded: float | None = None
    while time.monotonic() < deadline:
        if aggregate.decoded_buffers and first_decoded is None:
            first_decoded = time.monotonic() - started
        if aggregate.failures:
            break
        if target_decoded is not None and aggregate.decoded_buffers >= target_decoded:
            break
        now = time.monotonic()
        if sampler is not None and samples is not None and now >= next_sample:
            pids = [pid for pid in (backend.pid, peer.pid) if pid]
            samples.append(sampler.sample(pids, set(), now - started))
            next_sample = now + sample_interval_seconds
        if on_tick is not None:
            on_tick(backend, aggregate, now - started)
        time.sleep(0.05)
    result["observed_seconds"] = round(time.monotonic() - started, 3)
    result["first_decoded_seconds"] = round(first_decoded, 3) if first_decoded else None
    result["ice_state"] = backend.ice_state
    result["connection_state"] = backend.connection_state
    result.update(aggregate.summary())
    pid = backend.pid
    backend.stop()
    result["worker_returncode"] = backend.returncode
    result["worker_stopped_cleanly"] = backend.stopped_cleanly
    result["worker_dropped_samples"] = backend.worker_dropped_samples
    result["worker_reaped"] = pid is None or pid not in child_pids(os.getpid())
    return result


def _post_checks(baseline_fds: int | None, baseline_children: int) -> dict[str, Any]:
    me = os.getpid()
    children = child_pids(me)
    return {
        "fds_before": baseline_fds,
        "fds_after": fd_count(me),
        "children_before": baseline_children,
        "children_after": len(children),
        "zombies_after": sum(1 for pid in children if process_state(pid) == "Z"),
    }


def scenario_local(arguments: argparse.Namespace) -> dict[str, Any]:
    me = os.getpid()
    baseline_fds, baseline_children = fd_count(me), len(child_pids(me))
    with LocalSendingPeer(
        codec=arguments.codec, width=arguments.width, height=arguments.height, fps=arguments.fps
    ) as peer:
        session = run_session(
            peer,
            peer.answer_for,
            1,
            duration_seconds=arguments.duration or 30.0,
            config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
        )
        session["peer_sent_buffers"] = peer.stats().get("sent_buffers")
        session["answer_exchange_seconds"] = round(peer.answer_seconds or 0.0, 3)
    time.sleep(0.5)
    return {"sessions": [session], "post": _post_checks(baseline_fds, baseline_children)}


def scenario_whep(arguments: argparse.Namespace) -> dict[str, Any]:
    me = os.getpid()
    baseline_fds, baseline_children = fd_count(me), len(child_pids(me))
    with (
        LocalSendingPeer(
            codec=arguments.codec, width=arguments.width, height=arguments.height, fps=arguments.fps
        ) as peer,
        WhepFixtureServer() as server,
    ):
        bridge = WhepBridge(peer, server)
        session = run_session(
            peer,
            bridge.answer_for,
            1,
            duration_seconds=arguments.duration or 30.0,
            config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
        )
        session["whep_control_seconds"] = round(bridge.control_seconds or 0.0, 3)
        session["whep_session_resource"] = "present" if bridge.session_url else "absent"
        session["whep_teardown_ok"] = bridge.teardown()
        observations = server.observations()
        session["whep_requests"] = [obs.method for obs in observations]
        session["whep_bearer_in_header_only"] = all(
            obs.headers.get("authorization") == f"Bearer {SYNTHETIC_BEARER}"
            and SYNTHETIC_BEARER not in obs.path
            and SYNTHETIC_BEARER not in obs.query
            for obs in observations
        )
    time.sleep(0.5)
    return {"sessions": [session], "post": _post_checks(baseline_fds, baseline_children)}


def scenario_negatives(arguments: argparse.Namespace) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    expectations = {
        "malformed-answer": "WHEP_INVALID_ANSWER",
        "empty-answer": "WHEP_INVALID_ANSWER",
        "audio-answer": "WHEP_INVALID_ANSWER",
        "unsupported-codec": "WEBRTC_CODEC_UNSUPPORTED",
        "no-media": "NO_MEDIA",
    }
    for mode, expected in expectations.items():
        with LocalSendingPeer(
            codec=arguments.codec, width=640, height=360, fps=15, mode=mode
        ) as peer:
            session = run_session(
                peer,
                peer.answer_for,
                1,
                duration_seconds=20.0 if mode == "no-media" else 25.0,
                target_decoded=1,
                config=WebRtcBackendConfig(
                    codec=VideoCodec(arguments.codec), gather_timeout_seconds=10.0
                ),
            )
        observed = session.get("start_failure") or (session.get("failures") or [None])[0]
        if observed is None and session.get("decoded_buffers", 0) == 0:
            observed = "NO_MEDIA"
        cases.append({"mode": mode, "expected": expected, "observed": observed, "session": session})
        time.sleep(0.3)
    # Runtime-absent behaviour without uninstalling anything: inject an empty capability probe.
    absent = probe_webrtc_runtime(element_present=lambda _name: False)
    cases.append(
        {
            "mode": "runtime-absent-probe",
            "expected": "WEBRTC_RUNTIME_UNAVAILABLE",
            "observed": "WEBRTC_RUNTIME_UNAVAILABLE" if not absent.available else "AVAILABLE",
            "session": {"missing_elements": list(absent.missing_elements)},
        }
    )
    # Sender disappears mid-session.
    peer = LocalSendingPeer(codec=arguments.codec, width=640, height=360, fps=15)
    killed = run_session(
        peer,
        peer.answer_for,
        1,
        duration_seconds=18.0,
        config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
        on_tick=lambda _b, _e, elapsed: (
            peer.kill() if elapsed > 6 and peer.process.poll() is None else None
        ),
    )
    peer.close()
    cases.append(
        {
            "mode": "sender-disappears",
            "expected": "MEDIA_STOPS",
            "observed": "MEDIA_STOPS" if killed.get("decoded_buffers", 0) > 0 else "NO_MEDIA",
            "session": killed,
        }
    )
    return {"cases": cases}


def scenario_cycles(arguments: argparse.Namespace) -> dict[str, Any]:
    me = os.getpid()
    baseline_fds, baseline_children = fd_count(me), len(child_pids(me))
    sessions: list[dict[str, Any]] = []
    # A sending peer negotiates exactly once, so every connect/disconnect cycle gets a fresh one.
    for generation in range(1, 6):
        with LocalSendingPeer(
            codec=arguments.codec,
            width=arguments.width,
            height=arguments.height,
            fps=arguments.fps,
        ) as peer:
            sessions.append(
                run_session(
                    peer,
                    peer.answer_for,
                    generation,
                    duration_seconds=20.0,
                    target_decoded=30,
                    config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
                )
            )
        time.sleep(0.3)
    time.sleep(0.5)
    return {"sessions": sessions, "post": _post_checks(baseline_fds, baseline_children)}


def scenario_recovery(arguments: argparse.Namespace) -> dict[str, Any]:
    """Three controlled receiver-side failures: kill only VeoTrex's own media worker."""
    me = os.getpid()
    baseline_fds, baseline_children = fd_count(me), len(child_pids(me))
    cycles: list[dict[str, Any]] = []
    for generation in range(1, 4):
        # One peer for the session that gets killed, another for the renegotiated recovery.
        with (
            LocalSendingPeer(
                codec=arguments.codec,
                width=arguments.width,
                height=arguments.height,
                fps=arguments.fps,
            ) as peer,
            LocalSendingPeer(
                codec=arguments.codec,
                width=arguments.width,
                height=arguments.height,
                fps=arguments.fps,
            ) as recovery_peer,
        ):
            events: list[BackendEvent] = []
            backend = WebRtcMediaBackend(
                generation, peer.answer_for, WebRtcBackendConfig(codec=VideoCodec(arguments.codec))
            )
            backend.start(local_lease(generation), events.append)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if sum(len(e.decoded) for e in events if isinstance(e, MediaBatch)) >= 20:
                    break
                time.sleep(0.05)
            pid = backend.pid
            killed_at = time.monotonic()
            if pid:
                os.kill(pid, signal.SIGKILL)  # VeoTrex's own worker only
            exited = None
            while time.monotonic() - killed_at < 10:
                if any(type(e).__name__ == "BackendExited" for e in events):
                    exited = time.monotonic() - killed_at
                    break
                time.sleep(0.02)
            backend.stop()
            stale = [e for e in events if isinstance(e, MediaBatch) and e.generation != generation]
            recovery = run_session(
                recovery_peer,
                recovery_peer.answer_for,
                generation + 10,
                duration_seconds=25.0,
                target_decoded=20,
                config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
            )
            cycles.append(
                {
                    "generation": generation,
                    "detected_exit_seconds": round(exited, 3) if exited else None,
                    "stale_generation_batches": len(stale),
                    "recovery": recovery,
                    "recovery_seconds": recovery.get("first_decoded_seconds"),
                    "worker_reaped": pid is None or pid not in child_pids(os.getpid()),
                }
            )
            time.sleep(0.3)
    time.sleep(0.5)
    return {"cycles": cycles, "post": _post_checks(baseline_fds, baseline_children)}


def scenario_soak(arguments: argparse.Namespace) -> dict[str, Any]:
    me = os.getpid()
    baseline_fds, baseline_children = fd_count(me), len(child_pids(me))
    samples: list[dict[str, Any]] = []
    probe = ResourceProbe()
    with LocalSendingPeer(
        codec=arguments.codec, width=arguments.width, height=arguments.height, fps=arguments.fps
    ) as peer:
        session = run_session(
            peer,
            peer.answer_for,
            1,
            duration_seconds=arguments.duration or 900.0,
            config=WebRtcBackendConfig(codec=VideoCodec(arguments.codec)),
            sampler=probe,
            samples=samples,
            sample_interval_seconds=10.0,
        )
        session["peer_sent_buffers"] = peer.stats().get("sent_buffers")
    time.sleep(0.5)
    return {
        "sessions": [session],
        "resources": summarize_resources(samples),
        "resource_samples": samples,
        "post": _post_checks(baseline_fds, baseline_children),
    }


def post_ramp_drift(
    samples: list[dict[str, Any]], ramp_seconds: float = 120.0
) -> dict[str, int | None]:
    """Steady-state drift (last minus first) over samples taken after the start-up ramp.

    The receive chain is built on ``pad-added``, so the earliest samples predate the decoder
    entirely; comparing against them measures cold start rather than growth.
    """

    def series(extract: Any) -> list[int]:
        values: list[int] = []
        for sample in samples:
            if sample.get("offset_seconds", 0.0) < ramp_seconds:
                continue
            with contextlib.suppress(KeyError, IndexError, TypeError):
                value = extract(sample)
                if isinstance(value, int):
                    values.append(value)
        return values

    def drift(values: list[int]) -> int | None:
        return values[-1] - values[0] if len(values) >= 2 else None

    return {
        "samples": len(series(lambda s: s["edge_agent_rss_bytes"])),
        "worker_rss_bytes": drift(series(lambda s: s["workers"][0]["rss_bytes"])),
        "worker_fds": drift(series(lambda s: s["workers"][0]["fds"])),
        "worker_threads": drift(series(lambda s: s["workers"][0]["threads"])),
        "edge_agent_rss_bytes": drift(series(lambda s: s["edge_agent_rss_bytes"])),
        "edge_agent_fds": drift(series(lambda s: s["edge_agent_fds"])),
    }


def evaluate(scenario: str, report: dict[str, Any]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    post = report.get("post")
    if post:
        checks["no_fd_leak"] = post["fds_after"] == post["fds_before"]
        checks["no_child_leak"] = post["children_after"] == post["children_before"]
        checks["no_zombies"] = post["zombies_after"] == 0
    sessions = report.get("sessions", []) + [
        cycle["recovery"] for cycle in report.get("cycles", [])
    ]
    if sessions:
        checks["media_decoded"] = all(s.get("decoded_buffers", 0) > 0 for s in sessions)
        checks["hardware_decode"] = all(
            s.get("hardware_decoder") and s.get("nvmm") for s in sessions
        )
        checks["connected"] = all(s.get("connected") for s in sessions)
        checks["no_failures"] = all(not s.get("failures") for s in sessions)
        checks["workers_reaped"] = all(s.get("worker_reaped", True) for s in sessions)
        checks["no_timestamp_regressions"] = all(
            s.get("timestamp_regressions", 0) == 0 for s in sessions
        )
    if scenario == "whep":
        session = report["sessions"][0]
        checks["whep_bearer_header_only"] = bool(session.get("whep_bearer_in_header_only"))
        checks["whep_teardown"] = bool(session.get("whep_teardown_ok"))
    if scenario == "negatives":
        checks["negatives_match"] = all(
            case["observed"] == case["expected"] for case in report["cases"]
        )
    if scenario == "cycles":
        checks["five_cycles"] = len(report["sessions"]) == 5
    if scenario == "recovery":
        checks["three_recoveries"] = len(report["cycles"]) == 3
        checks["no_stale_generation"] = all(
            cycle["stale_generation_batches"] == 0 for cycle in report["cycles"]
        )
    if scenario == "soak":
        # The receive chain is only built on pad-added, so the first sample predates the decoder
        # and a first->max comparison measures cold-start ramp, not growth. Steady-state drift is
        # measured after the ramp; the raw ramp is still reported in "resources".
        drift = post_ramp_drift(report.get("resource_samples", []))
        report["steady_state_drift"] = drift
        checks["bounded_worker_rss_drift"] = (
            drift["worker_rss_bytes"] is not None and drift["worker_rss_bytes"] <= 8 * 1024 * 1024
        )
        checks["bounded_agent_rss_drift"] = (
            drift["edge_agent_rss_bytes"] is not None
            and drift["edge_agent_rss_bytes"] <= 8 * 1024 * 1024
        )
        checks["stable_worker_fds"] = drift["worker_fds"] is not None and drift["worker_fds"] <= 2
        checks["stable_worker_threads"] = (
            drift["worker_threads"] is not None and drift["worker_threads"] <= 2
        )
        checks["no_stalls"] = report["sessions"][0].get("stalls_over_2s", 0) == 0
    return checks


def _write(report: dict[str, Any], directory: Path, scenario: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"r5a-r2-webrtc-{scenario}-{stamp}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), "utf-8")
    os.replace(temporary, path)
    return path


def run_webrtc_cli(arguments: argparse.Namespace) -> int:  # pragma: no cover - hardware path
    runner = {
        "local": scenario_local,
        "whep": scenario_whep,
        "negatives": scenario_negatives,
        "cycles": scenario_cycles,
        "recovery": scenario_recovery,
        "soak": scenario_soak,
    }[arguments.scenario]
    runtime = probe_webrtc_runtime()
    report: dict[str, Any] = {
        "stage": "R5A-R2",
        "scenario": arguments.scenario,
        "webrtc_runtime": runtime.as_dict(),
        "media": "synthetic videotestsrc over loopback WebRTC; nothing retained",
        **runner(arguments),
    }
    report["checks"] = evaluate(arguments.scenario, report)
    report["passed"] = all(report["checks"].values())
    path = _write(report, arguments.report_dir, arguments.scenario)
    summary = {
        "report": str(path),
        "passed": report["passed"],
        "failed_checks": [key for key, ok in report["checks"].items() if not ok],
    }
    if report.get("sessions"):
        first = report["sessions"][0]
        summary["first_session"] = {
            key: first.get(key)
            for key in (
                "decoded_buffers",
                "decoded_fps",
                "first_decoded_seconds",
                "hardware_decoder",
                "nvmm",
                "p95_gap_ms",
            )
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1
