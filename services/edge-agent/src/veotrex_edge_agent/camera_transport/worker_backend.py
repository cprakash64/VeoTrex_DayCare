from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendExited,
    BackendFailed,
    DecodedCaps,
    EndOfStream,
    Heartbeat,
    MediaBatch,
    MediaNegotiated,
    TransportConnected,
)
from veotrex_edge_agent.camera_transport.descriptor import (
    CredentialMode,
    LiveSessionLease,
    VideoCodec,
)
from veotrex_edge_agent.camera_transport.errors import (
    TransportError,
    TransportErrorCategory,
    safe_category,
)
from veotrex_edge_agent.camera_transport.timing import CompressedSample, DecodedSample

SYSTEM_PYTHON = Path("/usr/bin/python3")
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
MAX_BATCH_SAMPLES = 400
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_SAFE_ENV = MappingProxyType({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
_EVENT_TYPES = frozenset(
    {"HELLO", "CONNECTED", "NEGOTIATED", "CAPS", "MEDIA", "HEARTBEAT", "FAILED", "EOS", "STOPPED"}
)


@dataclass(frozen=True, slots=True)
class WorkerBackendConfig:
    decoder: str = "nvidia"
    latency_ms: int = 200
    tcp_timeout_ms: int = 10_000
    hello_timeout_seconds: float = 15.0
    stop_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.decoder not in {"nvidia", "software", "none"}:
            raise ValueError("decoder must be nvidia, software, or none")
        if not 0 <= self.latency_ms <= 5000:
            raise ValueError("latency must be between 0 and 5000 ms")
        if not 1000 <= self.tcp_timeout_ms <= 60_000:
            raise ValueError("tcp timeout must be between 1 and 60 seconds")
        if not 0 < self.hello_timeout_seconds <= 60 or not 0 < self.stop_timeout_seconds <= 30:
            raise ValueError("worker timeouts are out of bounds")


def _int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("invalid integer field")
    return value


def _timestamp(value: object) -> int | None:
    number = _int(value, -1, 2**63 - 1)
    return None if number < 0 else number


class WorkerMediaBackend:
    """Parent side of one isolated GStreamer transport worker (one session generation).

    The worker is ``/usr/bin/python3 -I`` with a fixed argv and a minimal environment. The only
    credential path is the START message on an inherited AF_UNIX/SOCK_SEQPACKET socketpair, so
    ``/proc/<pid>/cmdline`` and ``/proc/<pid>/environ`` never contain it.
    """

    def __init__(
        self,
        generation: int,
        config: WorkerBackendConfig | None = None,
        *,
        executable: Path = SYSTEM_PYTHON,
        worker_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.generation = generation
        self.config = config or WorkerBackendConfig()
        self._executable = executable
        self._worker_path = worker_path or Path(__file__).with_name("media_worker.py")
        self._clock = clock
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False
        self._stopped_cleanly = False
        self.hello: dict[str, Any] | None = None
        self.protocol_errors = 0
        self.worker_dropped_samples = 0

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def argv(self) -> list[str]:
        return list(self._process.args) if self._process is not None else []  # type: ignore[arg-type]

    @property
    def environment(self) -> dict[str, str]:
        return dict(_SAFE_ENV)

    def _validated_paths(self) -> tuple[Path, Path]:
        executable = self._executable.resolve(strict=True)
        worker = self._worker_path.resolve(strict=True)
        package = Path(__file__).resolve().parent
        if executable != SYSTEM_PYTHON.resolve(strict=True):
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        if self._worker_path.is_symlink() or not worker.is_file() or worker.parent != package:
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        return executable, worker

    def start(self, lease: LiveSessionLease, emit: Callable[[BackendEvent], None]) -> None:
        descriptor = lease.descriptor
        if descriptor.generation != self.generation:
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        with self._lock:
            if self._stopping or self._process is not None:
                return
            executable, worker = self._validated_paths()
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                self._process = subprocess.Popen(  # noqa: S603 - fixed, validated argv only
                    [str(executable), "-I", str(worker), "--fd", str(child.fileno())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(child.fileno(),),
                    env=dict(_SAFE_ENV),
                    start_new_session=True,
                )
            except OSError:
                parent.close()
                raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR) from None
            finally:
                child.close()
            self._socket = parent
            try:
                parent.settimeout(self.config.hello_timeout_seconds)
                hello = self._receive(parent)
                if (
                    hello.get("type") != "HELLO"
                    or hello.get("protocol_version") != PROTOCOL_VERSION
                ):
                    raise ValueError("invalid hello")
                self.hello = {
                    key: hello.get(key) for key in ("worker_pid", "gstreamer_version", "plugins")
                }
                credential = lease.credential
                user_mode = credential.mode is CredentialMode.RTSP_USER_PASSWORD
                start = {
                    "type": "START",
                    "protocol_version": PROTOCOL_VERSION,
                    "generation": self.generation,
                    "location": descriptor.endpoint.uri,
                    "user_id": credential.username if user_mode else "",
                    "password": credential.secret.get_secret_value() if user_mode else "",
                    "latency_ms": self.config.latency_ms,
                    "tcp_timeout_ms": self.config.tcp_timeout_ms,
                    "decoder": self.config.decoder,
                }
                data = json.dumps(start, separators=(",", ":"), ensure_ascii=True).encode()
                start.clear()
                if len(data) > MAX_MESSAGE_BYTES:
                    raise ValueError("start message too large")
                parent.send(data)
                del data
                parent.settimeout(None)
            except (OSError, ValueError, TimeoutError):
                self._terminate_locked()
                raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR) from None
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(parent, emit),
                name=f"camera-transport-worker-{self.generation}",
                daemon=True,
            )
            self._reader.start()

    @staticmethod
    def _receive(sock: socket.socket) -> dict[str, Any]:
        data = sock.recv(MAX_MESSAGE_BYTES + 1)
        if not data:
            raise EOFError
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise ValueError("invalid envelope")
        return value

    def _read_loop(self, sock: socket.socket, emit: Callable[[BackendEvent], None]) -> None:
        while True:
            try:
                message = self._receive(sock)
            except (EOFError, OSError):
                break
            except (ValueError, UnicodeError):
                self.protocol_errors += 1
                if self.protocol_errors > 16:
                    emit(
                        BackendFailed(
                            self.generation,
                            self._clock(),
                            TransportErrorCategory.INTERNAL_TRANSPORT_ERROR,
                        )
                    )
                    break
                continue
            try:
                event = self.translate(message)
            except (ValueError, TypeError, KeyError):
                self.protocol_errors += 1
                continue
            if event is not None:
                emit(event)
        if not self._stopping:
            emit(BackendExited(self.generation, self._clock()))

    def translate(self, message: dict[str, Any]) -> BackendEvent | None:
        kind = message.get("type")
        if kind not in _EVENT_TYPES:
            raise ValueError("unknown event")
        generation = message.get("generation")
        if kind == "FAILED" and generation == 0:
            generation = self.generation
        if kind != "HELLO" and generation != self.generation:
            raise ValueError("generation mismatch")

        def at() -> float:
            raw = message.get("at_ns")
            return self._clock() if raw is None else _int(raw, 0, 2**63 - 1) / 1e9

        if kind == "CONNECTED":
            return TransportConnected(self.generation, at())
        if kind == "NEGOTIATED":
            decoder = message.get("decoder")
            if decoder is not None and decoder not in {
                "nvv4l2decoder",
                "openh264dec",
                "avdec_h264",
                "avdec_h265",
            }:
                raise ValueError("unknown decoder")
            return MediaNegotiated(
                self.generation,
                at(),
                VideoCodec(message["codec"]),
                decoder,
                message.get("hardware_decoder") is True,
            )
        if kind == "CAPS":
            framerate = message.get("framerate")
            if framerate is not None and (
                not isinstance(framerate, int | float) or not 0 <= framerate <= 1000
            ):
                raise ValueError("invalid framerate")
            width, height = message.get("width"), message.get("height")
            return DecodedCaps(
                self.generation,
                at(),
                None if width is None else _int(width, 1, 16_384),
                None if height is None else _int(height, 1, 16_384),
                None if framerate is None else float(framerate),
                message.get("nvmm") is True,
            )
        if kind == "MEDIA":
            compressed_raw, decoded_raw = message.get("compressed"), message.get("decoded")
            if not isinstance(compressed_raw, list) or not isinstance(decoded_raw, list):
                raise ValueError("invalid media batch")
            if len(compressed_raw) > MAX_BATCH_SAMPLES or len(decoded_raw) > MAX_BATCH_SAMPLES:
                raise ValueError("media batch too large")
            compressed = tuple(
                CompressedSample(
                    _int(item[0], 0, 2**63 - 1) / 1e9,
                    _timestamp(item[1]),
                    _timestamp(item[2]),
                    _int(item[3], 0, MAX_SAMPLE_BYTES),
                )
                for item in compressed_raw
                if isinstance(item, list) and len(item) == 4
            )
            decoded = tuple(
                DecodedSample(_int(item[0], 0, 2**63 - 1) / 1e9, _timestamp(item[1]))
                for item in decoded_raw
                if isinstance(item, list) and len(item) == 2
            )
            return MediaBatch(self.generation, compressed, decoded)
        if kind == "HEARTBEAT":
            dropped = message.get("dropped_samples", 0)
            self.worker_dropped_samples = _int(dropped, 0, 2**31)
            return Heartbeat(self.generation, at())
        if kind == "FAILED":
            return BackendFailed(self.generation, at(), safe_category(message.get("category")))
        if kind == "EOS":
            return EndOfStream(self.generation, at())
        if kind == "STOPPED":
            self._stopped_cleanly = True
        return None

    def _terminate_locked(self) -> None:
        sock, process = self._socket, self._process
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        if process is not None:
            try:
                process.wait(timeout=self.config.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.config.stop_timeout_seconds)
        if sock is not None:
            sock.close()
            self._socket = None

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            sock, process = self._socket, self._process
            if sock is not None and process is not None and process.poll() is None:
                with contextlib.suppress(OSError):
                    sock.send(b'{"type":"STOP"}')
                try:
                    process.wait(timeout=self.config.stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.config.stop_timeout_seconds)
            self._terminate_locked()
            reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self.config.stop_timeout_seconds)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def stopped_cleanly(self) -> bool:
        return self._stopped_cleanly
