"""V1-DEMO-03C synthetic end-to-end: real pixels through the brokered Ring live path.

    loopback "VeoTrex broker" (WhepFixtureServer)   <- real BrokerWhepClient, edge credential file
        -> SDP answer from a local WebRTC sending peer (videotestsrc, H.264, loopback ICE)
        -> real webrtc_worker.py: webrtcbin -> depay -> parse -> decode -> BGR -> bounded appsink
        -> sealed memfd -> GstFrameReader -> RingWhepSource -> LiveDemoRuntime / preview

Nothing here contacts Ring, the production broker or any camera, and no recorded footage is
used: the only picture is GStreamer's moving test ball. Every test is bounded by the reader's
own timeouts, the reconnect budget, and a watchdog that closes the source at a hard deadline.
Skipped where the system GStreamer/WebRTC stack is absent, so CI needs no NVIDIA hardware; the
software decoder is used whenever NVDEC is missing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pytest

from veotrex_edge_agent.camera_transport.reconnect import ReconnectPolicy
from veotrex_edge_agent.live import LiveDemoRuntime, PreviewConfig, PreviewRenderer
from veotrex_edge_agent.live.cli import build_brokered_ring_source
from veotrex_edge_agent.live.ring import RingWhepSource
from veotrex_edge_agent.live.ring_gst import GstFrameReader
from veotrex_edge_agent.live.source import LiveFrame, SourceHealth, SourceKind
from veotrex_edge_agent.qualification.transport_qualification import child_pids
from veotrex_edge_agent.qualification.webrtc_qualification import LocalSendingPeer
from veotrex_edge_agent.qualification.whep_fixture_server import (
    Observation,
    WhepFixtureServer,
    WhepScript,
)
from veotrex_edge_agent.recorded.detector import FakePersonDetector

SYSTEM_PYTHON = Path("/usr/bin/python3")
CAMERA = UUID("6b1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f")
TOKEN = "vte1.1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d." + "S" * 42 + "w"  # obviously synthetic
LEASE_PREFIX = "/v1/edge/whep-leases/"
WIDTH, HEIGHT = 320, 240


def _stack() -> str | None:
    """The decoder mode this host can run the real route with, or None to skip."""
    if not SYSTEM_PYTHON.exists():
        return None
    probe = (
        "import gi; gi.require_version('Gst','1.0')\n"
        "from gi.repository import Gst; Gst.init(None)\n"
        "need = ('webrtcbin','nicesrc','nicesink','x264enc','videotestsrc','rtph264depay',"
        "'h264parse','videoconvert','appsink')\n"
        "if not all(Gst.ElementFactory.find(n) for n in need): raise SystemExit(1)\n"
        "print('nvidia' if Gst.ElementFactory.find('nvv4l2decoder') else "
        "'software' if (Gst.ElementFactory.find('avdec_h264') or "
        "Gst.ElementFactory.find('openh264dec')) else 'none')\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal probe
        [str(SYSTEM_PYTHON), "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    mode = result.stdout.strip()
    return mode if result.returncode == 0 and mode in {"nvidia", "software"} else None


DECODER = _stack()
pytestmark = pytest.mark.skipif(DECODER is None, reason="system GStreamer/WebRTC stack absent")


class PeerBroker:
    """The loopback broker: every POST gets a fresh synthetic sending peer's real answer."""

    def __init__(self, server: WhepFixtureServer, modes: list[str] | None = None) -> None:
        self.server = server
        self.modes = list(modes or [])
        self.peers: list[LocalSendingPeer] = []
        self._lock = threading.Lock()
        server.state.responder = self.respond

    def respond(self, observation: Observation) -> WhepScript:
        if observation.headers.get("authorization") != f"Bearer {TOKEN}":
            return WhepScript(status=401, body=b"", content_type=None, require_bearer=False)
        if observation.method == "DELETE":
            return WhepScript(status=204, body=b"", content_type=None, require_bearer=False)
        with self._lock:
            mode = self.modes.pop(0) if self.modes else "normal"
            peer = LocalSendingPeer(width=WIDTH, height=HEIGHT, fps=15, mode=mode)
            self.peers.append(peer)
        answer = peer.answer_for(observation.body.decode(), None)  # type: ignore[arg-type]
        lease = LEASE_PREFIX + f"{len(self.peers):043d}"
        return WhepScript(201, answer.encode(), location=lease, require_bearer=False)

    def posts(self) -> list[Observation]:
        return [o for o in self.server.observations() if o.method == "POST"]

    def deletes(self) -> list[Observation]:
        return [o for o in self.server.observations() if o.method == "DELETE"]

    def close(self) -> None:
        for peer in self.peers:
            peer.close()


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "edge.credential"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(TOKEN + "\n")
    return path


@contextmanager
def brokered(
    credential_file: Path,
    *,
    modes: list[str] | None = None,
    policy: ReconnectPolicy | None = None,
    first_frame_timeout: float = 20.0,
    stall_timeout: float = 3.0,
    hard_deadline: float = 90.0,
) -> Iterator[tuple[RingWhepSource, PeerBroker]]:
    with WhepFixtureServer() as server:
        broker = PeerBroker(server, modes)
        assert DECODER is not None
        source = build_brokered_ring_source(
            str(CAMERA),
            control_plane_url="https://127.0.0.1:443",
            credential_file=str(credential_file),
            environment="test",
            connection_factory=server.connection_factory(),
            reader=GstFrameReader(source="webrtc", decoder=DECODER),
            reconnect_policy=policy
            or ReconnectPolicy(
                initial_delay_seconds=0.05, maximum_delay_seconds=0.1, max_attempts=2
            ),
            first_frame_timeout_seconds=first_frame_timeout,
            stall_timeout_seconds=stall_timeout,
        )
        watchdog = threading.Timer(hard_deadline, source.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            yield source, broker
        finally:
            watchdog.cancel()
            source.close()
            broker.close()


def _no_workers(broker: PeerBroker, timeout: float = 8.0) -> bool:
    """No media worker survives: every child left is a synthetic sending peer."""
    peers = {peer.pid for peer in broker.peers}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not set(child_pids(os.getpid())) - peers:
            return True
        time.sleep(0.05)
    return False


def _take(source: RingWhepSource, count: int) -> list[LiveFrame]:
    frames: list[LiveFrame] = []
    for frame in source.frames():
        frames.append(frame)
        if len(frames) >= count:
            break
    return frames


# ------------------------------------------------------------------------------ happy path
def test_real_pixels_traverse_brokered_webrtc_into_the_ring_source(
    credential_file: Path,
) -> None:
    threads_before = threading.active_count()
    with brokered(credential_file) as (source, broker):
        started = time.monotonic()
        frames = _take(source, 12)
        elapsed = time.monotonic() - started
        source.close()
        assert _no_workers(broker), "no media worker survives close()"
        assert len(frames) == 12, f"only {len(frames)} frames in {elapsed:.1f}s"
        for frame in frames:
            assert frame.kind is SourceKind.LIVE_RING_WHEP
            assert (frame.width, frame.height) == (WIDTH, HEIGHT)
            assert frame.image.shape == (HEIGHT, WIDTH, 3) and frame.image.dtype == np.uint8
            assert frame.image.nbytes == WIDTH * HEIGHT * 3
            assert frame.image.flags.owndata, "the pipeline owns a copy, not a mapping"
        assert [f.frame_index for f in frames] == list(range(12))
        stamps = [f.monotonic_ns for f in frames]
        assert stamps == sorted(stamps) and len(set(stamps)) == 12
        assert frames[0].discontinuity is True
        # Real decoded pictures: textured, and the test ball moves between frames.
        assert all(float(f.image.std()) > 5.0 for f in frames)
        assert np.any(frames[0].image != frames[-1].image)
        [post] = broker.posts()
        assert post.path == f"/v1/edge/cameras/{CAMERA}/whep"
        assert post.headers["authorization"] == f"Bearer {TOKEN}"
        offer = post.body.decode()
        assert offer.startswith("v=0") and "a=recvonly" in offer
        assert [line for line in offer.splitlines() if line.startswith("m=")] and all(
            line.startswith("m=video") for line in offer.splitlines() if line.startswith("m=")
        )
        [delete] = broker.deletes()
        assert delete.path.startswith(LEASE_PREFIX)
        assert source.health is SourceHealth.STOPPED and source.failure_category is None
        stats = source.snapshot()
        assert stats["ring_frames_received_total"] >= 12
        assert stats["ring_whep_sessions_completed_total"] == 1
    assert threading.active_count() <= threads_before + 1  # the fixture server thread at most


def test_the_existing_live_runtime_and_preview_consume_ring_frames(
    credential_file: Path,
) -> None:
    with brokered(credential_file) as (source, broker):
        preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
        runtime = LiveDemoRuntime(source, FakePersonDetector({}), preview=preview)
        # Exactly as the dashboard runs it: in the background, observed while live. The
        # runtime clears the preview when it stops, so a dead feed is never shown as live.
        runtime.start()
        latest = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            latest = preview.buffer.latest()
            if latest is not None and runtime.metrics()["video_frames_processed_total"] >= 10:
                break
            time.sleep(0.05)
        metrics = runtime.metrics()
        runtime.stop()
        assert runtime.failure is None
        assert metrics["video_frames_processed_total"] >= 10
        assert metrics["source_health"] == "RUNNING"
        assert latest is not None and latest.jpeg[:2] == b"\xff\xd8"
        assert (latest.width, latest.height) == (WIDTH, HEIGHT)
        assert preview.buffer.latest() is None, "stopping clears the preview"
        assert _no_workers(broker)
        assert len(broker.deletes()) == len(broker.posts()) == 1


# ------------------------------------------------------------------ failure and recovery
def test_a_stall_after_frames_reconnects_with_a_fresh_worker_and_session(
    credential_file: Path,
) -> None:
    with brokered(credential_file, stall_timeout=1.5) as (source, broker):
        before: list[LiveFrame] = []
        after: list[LiveFrame] = []
        for frame in source.frames():
            if len(before) < 5:
                before.append(frame)
                if len(before) == 5:
                    broker.peers[0].stall(60)
                continue
            if frame.discontinuity or after:
                after.append(frame)
            if len(after) >= 5:
                break
        source.close()
        assert len(before) == 5 and len(after) == 5
        assert after[0].discontinuity is True, "a reconnect breaks temporal continuity"
        assert source.reconnect_count == 1
        assert len(broker.posts()) == 2 and len(broker.deletes()) == 2
        assert _no_workers(broker)


def test_first_frame_never_arriving_fails_boundedly(credential_file: Path) -> None:
    policy = ReconnectPolicy(initial_delay_seconds=0.05, maximum_delay_seconds=0.1, max_attempts=1)
    with brokered(
        credential_file, modes=["no-media", "no-media"], policy=policy, first_frame_timeout=4.0
    ) as (source, broker):
        started = time.monotonic()
        assert list(source.frames()) == []
        assert time.monotonic() - started < 45
        assert source.health is SourceHealth.FAILED
        assert source.failure_category == "FIRST_MEDIA_TIMEOUT"
        assert len(broker.posts()) == 2, "one attempt plus exactly one bounded retry"
        assert len(broker.deletes()) == 2
        assert _no_workers(broker)


def test_the_worker_dying_mid_stream_is_recovered_with_a_new_worker(
    credential_file: Path,
) -> None:
    with brokered(credential_file) as (source, broker):
        reader = source._reader
        pids: list[int] = []
        frames: list[LiveFrame] = []
        for frame in source.frames():
            frames.append(frame)
            process = reader._process
            if process is not None and (not pids or pids[-1] != process.pid):
                pids.append(process.pid)
            if len(frames) == 3:
                os.kill(pids[0], signal.SIGKILL)
            if len(pids) == 2 and len(frames) >= 6:
                break
        source.close()
        assert len(pids) == 2 and pids[0] != pids[1]
        assert source.reconnect_count == 1
        assert len(broker.posts()) == len(broker.deletes()) == 2
        assert _no_workers(broker)


def test_a_malformed_broker_answer_ends_boundedly_without_leaking(
    credential_file: Path,
) -> None:
    policy = ReconnectPolicy(initial_delay_seconds=0.05, maximum_delay_seconds=0.1, max_attempts=1)
    with brokered(
        credential_file, modes=["malformed-answer", "malformed-answer"], policy=policy
    ) as (source, broker):
        assert list(source.frames()) == []
        assert source.health is SourceHealth.FAILED
        assert source.failure_category == "WHEP_INVALID_ANSWER"
        assert len(broker.posts()) == 2
        assert broker.deletes() == [], "the client refused the answer, so no lease is held"
        assert _no_workers(broker)


def test_close_from_another_thread_ends_a_streaming_source(credential_file: Path) -> None:
    with brokered(credential_file) as (source, broker):
        received: list[Any] = []

        def consume() -> None:
            for frame in source.frames():
                received.append(frame.frame_index)

        thread = threading.Thread(target=consume)
        thread.start()
        deadline = time.monotonic() + 30
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        source.close()
        thread.join(timeout=10)
        assert not thread.is_alive(), "the capture thread exits once the source is closed"
        assert len(received) >= 3
        assert len(broker.deletes()) == len(broker.posts()) == 1
        assert _no_workers(broker)
