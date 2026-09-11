import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

import veotrex_edge_agent.qualification as qualification_package
from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendFailed,
    DecodedCaps,
    MediaBatch,
    MediaNegotiated,
)
from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    CredentialMode,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    SessionCredential,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.worker_backend import (
    WorkerBackendConfig,
    WorkerMediaBackend,
)

SYSTEM_PYTHON = Path("/usr/bin/python3")
pytestmark = pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="system Python is unavailable")
CAMERA = UUID(int=0x5A)


def _gst_media_available() -> bool:
    if not SYSTEM_PYTHON.exists():
        return False
    probe = (
        "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None);"
        "import sys; sys.exit(0 if all(Gst.ElementFactory.find(n) for n in "
        "('rtspsrc','rtph264depay','h264parse','nvv4l2decoder','x264enc','videotestsrc')) else 1)"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal probe
        [str(SYSTEM_PYTHON), "-I", "-c", probe], capture_output=True, timeout=30, check=False
    )
    return result.returncode == 0


GST_MEDIA = _gst_media_available()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _lease(port: int, secret: str, generation: int = 1) -> LiveSessionLease:
    descriptor = LiveSessionDescriptor(
        provider=ProviderKind.LOCAL_FIXTURE,
        logical_camera_id=CAMERA,
        generation=generation,
        endpoint=validate_endpoint(
            f"rtsp://127.0.0.1:{port}/stream", LOCAL_FIXTURE_ENDPOINT_POLICY
        ),
        created_monotonic=time.monotonic(),
    )
    credential = SessionCredential(
        CredentialMode.RTSP_USER_PASSWORD, "veotrex-fixture", SecretStr(secret)
    )
    return LiveSessionLease(descriptor, credential)


class Collector:
    def __init__(self) -> None:
        self.events: list[BackendEvent] = []
        self._condition = threading.Condition()

    def __call__(self, event: BackendEvent) -> None:
        with self._condition:
            self.events.append(event)
            self._condition.notify_all()

    def wait_for(self, predicate, timeout: float) -> bool:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate(self.events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def _assert_reaped(pid: int) -> None:
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    status = Path(f"/proc/{pid}/status")
    if status.exists():  # PID reuse by an unrelated process is possible but never our zombie.
        assert "State:\tZ" not in status.read_text()


def test_worker_secret_never_in_argv_or_environment_and_resources_are_released() -> None:
    secret = "synthetic-" + secrets.token_hex(16)
    before = _fds()
    collector = Collector()
    backend = WorkerMediaBackend(1, WorkerBackendConfig(tcp_timeout_ms=2000))
    backend.start(_lease(_closed_port(), secret), collector)
    pid = backend.pid
    assert pid is not None
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    environ = Path(f"/proc/{pid}/environ").read_bytes()
    assert secret.encode() not in cmdline and secret.encode() not in environ
    assert b"127.0.0.1" not in cmdline
    assert set(environ.split(b"\0")) - {b""} == {
        b"PATH=/usr/bin:/bin",
        b"LANG=C.UTF-8",
        b"LC_ALL=C.UTF-8",
    }
    assert collector.wait_for(
        lambda events: any(isinstance(e, BackendFailed) for e in events), timeout=30
    )
    failure = next(e for e in collector.events if isinstance(e, BackendFailed))
    expected = C.TRANSPORT_CONNECT_FAILED if GST_MEDIA else C.DECODER_START_FAILED
    assert failure.category is expected
    backend.stop()
    assert backend.pid is None and backend.returncode is not None
    _assert_reaped(pid)
    assert _fds() == before


def test_stop_during_connecting_kills_and_reaps_without_leaks() -> None:
    before = _fds()
    for generation in range(1, 4):
        backend = WorkerMediaBackend(generation, WorkerBackendConfig(stop_timeout_seconds=2))
        backend.start(
            _lease(_closed_port(), "synthetic-" + secrets.token_hex(8), generation), Collector()
        )
        pid = backend.pid
        backend.stop()
        backend.stop()  # idempotent
        assert pid is not None
        _assert_reaped(pid)
    assert _fds() == before


def test_worker_path_and_generation_are_validated(tmp_path: Path) -> None:
    lease = _lease(_closed_port(), "synthetic-secret-abcdef")
    with pytest.raises(TransportError):
        WorkerMediaBackend(2).start(lease, Collector())
    rogue = tmp_path / "media_worker.py"
    rogue.write_text("print('x')")
    with pytest.raises(TransportError):
        WorkerMediaBackend(1, worker_path=rogue).start(lease, Collector())
    with pytest.raises(ValueError):
        WorkerBackendConfig(decoder="ffmpeg")


def test_worker_event_translation_rejects_untrusted_shapes() -> None:
    backend = WorkerMediaBackend(7)
    batch = backend.translate(
        {
            "type": "MEDIA",
            "generation": 7,
            "compressed": [[1_000_000_000, -1, 5, 100]],
            "decoded": [[1_000_000_001, 42]],
        }
    )
    assert isinstance(batch, MediaBatch)
    assert batch.compressed[0].pts_ns is None and batch.decoded[0].pts_ns == 42
    caps = backend.translate(
        {
            "type": "CAPS",
            "generation": 7,
            "width": 1920,
            "height": 1080,
            "framerate": 15.0,
            "nvmm": True,
        }
    )
    assert isinstance(caps, DecodedCaps) and caps.nvmm
    negotiated = backend.translate(
        {
            "type": "NEGOTIATED",
            "generation": 7,
            "codec": "H265",
            "decoder": "nvv4l2decoder",
            "hardware_decoder": True,
        }
    )
    assert isinstance(negotiated, MediaNegotiated)
    failed = backend.translate({"type": "FAILED", "generation": 7, "category": "rtsp://u:p@h/x"})
    assert isinstance(failed, BackendFailed) and failed.category is C.INTERNAL_TRANSPORT_ERROR
    for bad in (
        {"type": "EXEC", "generation": 7},
        {"type": "MEDIA", "generation": 6, "compressed": [], "decoded": []},
        {"type": "MEDIA", "generation": 7, "compressed": [[True, 1, 1, 1]], "decoded": []},
        {"type": "MEDIA", "generation": 7, "compressed": [[1, 1, 1, 1]] * 401, "decoded": []},
        {"type": "CAPS", "generation": 7, "width": 10**9, "height": 1, "framerate": 1},
        {"type": "NEGOTIATED", "generation": 7, "codec": "VP9", "decoder": None},
        {"type": "NEGOTIATED", "generation": 7, "codec": "H264", "decoder": "filesink"},
    ):
        with pytest.raises((ValueError, TypeError, KeyError)):
            backend.translate(bad)


# --------------------------------------------------------------------- loopback integration
@pytest.fixture
def fixture_server() -> Iterator[dict[str, object]]:
    password = "synthetic-" + secrets.token_urlsafe(18)
    path = Path(qualification_package.__file__).with_name("rtsp_fixture_server.py")
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and repository script
        [str(SYSTEM_PYTHON), "-I", str(path), "--width", "640", "--height", "360", "--fps", "15"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(password + "\n")
    process.stdin.flush()
    ready = json.loads(process.stdout.readline())

    def command(text: str) -> dict[str, object]:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(text + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())  # type: ignore[no-any-return]

    try:
        yield {"port": ready["port"], "password": password, "command": command}
    finally:
        process.stdin.write("quit\n")
        process.stdin.flush()
        process.wait(timeout=15)


@pytest.mark.skipif(not GST_MEDIA, reason="GStreamer RTSP/NVIDIA decode stack unavailable")
def test_loopback_rtsp_decodes_on_nvidia_hardware(fixture_server: dict[str, object]) -> None:
    collector = Collector()
    backend = WorkerMediaBackend(1)
    backend.start(
        _lease(int(fixture_server["port"]), str(fixture_server["password"])),  # type: ignore[call-overload]
        collector,
    )
    try:
        assert collector.wait_for(
            lambda events: sum(len(e.decoded) for e in events if isinstance(e, MediaBatch)) >= 10,
            timeout=30,
        )
    finally:
        backend.stop()
    negotiated = next(e for e in collector.events if isinstance(e, MediaNegotiated))
    caps = next(e for e in collector.events if isinstance(e, DecodedCaps))
    assert negotiated.decoder == "nvv4l2decoder" and negotiated.hardware_decoder
    assert caps.nvmm and (caps.width, caps.height) == (640, 360)
    assert not any(isinstance(e, BackendFailed) for e in collector.events)


@pytest.mark.skipif(not GST_MEDIA, reason="GStreamer RTSP stack unavailable")
@pytest.mark.parametrize("attack", ["redirect", "cross-origin-control"])
def test_redirects_and_cross_origin_control_urls_are_refused_without_credential_leak(
    fixture_server: dict[str, object], attack: str
) -> None:
    command = fixture_server["command"]
    command(f"{attack} on")  # type: ignore[operator]
    collector = Collector()
    backend = WorkerMediaBackend(1)
    backend.start(
        _lease(int(fixture_server["port"]), str(fixture_server["password"])),  # type: ignore[call-overload]
        collector,
    )
    try:
        assert collector.wait_for(
            lambda events: any(isinstance(e, BackendFailed) for e in events), timeout=30
        )
    finally:
        backend.stop()
    failure = next(e for e in collector.events if isinstance(e, BackendFailed))
    assert failure.category is C.REDIRECT_REFUSED
    stats = command("stats")  # type: ignore[operator]
    # No RTSP request, and therefore no credential, ever reaches the foreign origin.
    assert stats["canary_auth_headers"] == 0
    if attack == "cross-origin-control":
        assert stats["canary_connections"] == 0
    else:
        # Documented residual: rtspsrc opens the redirect target's TCP connection before the
        # first request can be refused by before-send. It sends zero bytes and fails closed.
        assert stats["canary_connections"] <= 1
    assert not any(isinstance(e, MediaBatch) and e.decoded for e in collector.events)


@pytest.mark.skipif(not GST_MEDIA, reason="GStreamer RTSP stack unavailable")
def test_wrong_credential_is_authorization_failure(fixture_server: dict[str, object]) -> None:
    collector = Collector()
    backend = WorkerMediaBackend(1)
    backend.start(_lease(int(fixture_server["port"]), "synthetic-wrong-password-123"), collector)  # type: ignore[call-overload]
    try:
        assert collector.wait_for(
            lambda events: any(isinstance(e, BackendFailed) for e in events), timeout=30
        )
    finally:
        backend.stop()
    failure = next(e for e in collector.events if isinstance(e, BackendFailed))
    assert failure.category is C.AUTHORIZATION_FAILED
