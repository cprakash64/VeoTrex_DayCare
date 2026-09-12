"""Parent side of the isolated WebRTC media worker (R5A-R2).

Bridges the R5A media-backend contract to a `webrtc_worker.py` child: the worker produces a
complete SDP offer, an injected exchange turns it into an answer (the Ring WHEP client in
production, the local synthetic peer in qualification), and the answer goes back to the worker.

The exchange is the only component that ever sees a credential; the media worker never does.
"""

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
from veotrex_edge_agent.camera_transport.descriptor import LiveSessionLease, VideoCodec
from veotrex_edge_agent.camera_transport.errors import (
    TransportError,
    TransportErrorCategory,
    safe_category,
)
from veotrex_edge_agent.camera_transport.timing import CompressedSample, DecodedSample

SYSTEM_PYTHON = Path("/usr/bin/python3")
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
MAX_SDP_BYTES = 61_440
MAX_BATCH_SAMPLES = 400
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_SAFE_ENV = MappingProxyType({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
_EVENT_TYPES = frozenset(
    {
        "HELLO",
        "OFFER",
        "CONNECTED",
        "NEGOTIATED",
        "CAPS",
        "MEDIA",
        "HEARTBEAT",
        "FAILED",
        "EOS",
        "STOPPED",
    }
)

# offer SDP -> answer SDP; raises TransportError with a taxonomy category on failure.
AnswerExchange = Callable[[str, LiveSessionLease], str]


@dataclass(frozen=True, slots=True)
class WebRtcBackendConfig:
    codec: VideoCodec = VideoCodec.H264
    decoder: str = "nvidia"
    stun_server: str | None = None
    gather_timeout_seconds: float = 15.0
    hello_timeout_seconds: float = 20.0
    offer_timeout_seconds: float = 30.0
    stop_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.decoder not in {"nvidia", "software", "none"}:
            raise ValueError("decoder must be nvidia, software, or none")
        if self.stun_server is not None and not self.stun_server.startswith("stun://"):
            raise ValueError("stun server must be a stun:// URI")
        if not 1 <= self.gather_timeout_seconds <= 60:
            raise ValueError("gather timeout is out of bounds")
        if not 0 < self.hello_timeout_seconds <= 120 or not 0 < self.offer_timeout_seconds <= 120:
            raise ValueError("worker timeouts are out of bounds")


def _int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("invalid integer field")
    return value


def _timestamp(value: object) -> int | None:
    number = _int(value, -1, 2**63 - 1)
    return None if number < 0 else number


class WebRtcMediaBackend:
    """One isolated WebRTC receive session (one generation)."""

    def __init__(
        self,
        generation: int,
        exchange: AnswerExchange,
        config: WebRtcBackendConfig | None = None,
        *,
        executable: Path = SYSTEM_PYTHON,
        worker_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.generation = generation
        self.config = config or WebRtcBackendConfig()
        self._exchange = exchange
        self._executable = executable
        self._worker_path = worker_path or Path(__file__).with_name("webrtc_worker.py")
        self._clock = clock
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False
        self.hello: dict[str, Any] | None = None
        self.offer_candidates: int | None = None
        self.protocol_errors = 0
        self.worker_dropped_samples = 0
        self.ice_state = ""
        self.connection_state = ""
        self.offer_at: float | None = None
        self.answer_at: float | None = None
        self._stopped_cleanly = False

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def stopped_cleanly(self) -> bool:
        return self._stopped_cleanly

    def _validated_paths(self) -> tuple[Path, Path]:
        executable = self._executable.resolve(strict=True)
        worker = self._worker_path.resolve(strict=True)
        package = Path(__file__).resolve().parent
        if executable != SYSTEM_PYTHON.resolve(strict=True):
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        if self._worker_path.is_symlink() or not worker.is_file() or worker.parent != package:
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        return executable, worker

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

    def _send(self, sock: socket.socket, value: dict[str, Any]) -> None:
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        if len(data) > MAX_MESSAGE_BYTES:
            raise TransportError(TransportErrorCategory.WHEP_OFFER_FAILED)
        sock.send(data)

    def start(self, lease: LiveSessionLease, emit: Callable[[BackendEvent], None]) -> None:
        if lease.descriptor.generation != self.generation:
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
                plugins = hello.get("plugins")
                if isinstance(plugins, dict) and not all(plugins.values()):
                    raise TransportError(TransportErrorCategory.WEBRTC_RUNTIME_UNAVAILABLE)
                self._send(
                    parent,
                    {
                        "type": "START",
                        "protocol_version": PROTOCOL_VERSION,
                        "generation": self.generation,
                        "codec": self.config.codec.value,
                        "decoder": self.config.decoder,
                        "stun_server": self.config.stun_server,
                        "gather_timeout_seconds": self.config.gather_timeout_seconds,
                    },
                )
                parent.settimeout(self.config.offer_timeout_seconds)
                offer = self._await_offer(parent)
            except TransportError:
                self._terminate_locked()
                raise
            except (OSError, ValueError, TimeoutError, EOFError):
                self._terminate_locked()
                raise TransportError(TransportErrorCategory.WEBRTC_NEGOTIATION_FAILED) from None
            try:
                answer = self._exchange(offer, lease)
            except TransportError:
                self._terminate_locked()
                raise
            except Exception:
                self._terminate_locked()
                raise TransportError(TransportErrorCategory.WHEP_OFFER_FAILED) from None
            if (
                not isinstance(answer, str)
                or not answer.startswith("v=0")
                or len(answer.encode()) > MAX_SDP_BYTES
            ):
                self._terminate_locked()
                raise TransportError(TransportErrorCategory.WHEP_INVALID_ANSWER)
            self.answer_at = self._clock()
            try:
                self._send(parent, {"type": "ANSWER", "sdp": answer})
                parent.settimeout(None)
            except OSError:
                self._terminate_locked()
                raise TransportError(TransportErrorCategory.WEBRTC_NEGOTIATION_FAILED) from None
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(parent, emit),
                name=f"webrtc-media-{self.generation}",
                daemon=True,
            )
            self._reader.start()

    def _await_offer(self, sock: socket.socket) -> str:
        while True:
            message = self._receive(sock)
            kind = message.get("type")
            if kind == "OFFER":
                sdp = message.get("sdp")
                if (
                    not isinstance(sdp, str)
                    or not sdp.startswith("v=0")
                    or len(sdp.encode()) > MAX_SDP_BYTES
                ):
                    raise TransportError(TransportErrorCategory.WEBRTC_NEGOTIATION_FAILED)
                self.offer_at = self._clock()
                with contextlib.suppress(ValueError):
                    self.offer_candidates = _int(message.get("candidates", 0), 0, 10_000)
                return sdp
            if kind == "FAILED":
                raise TransportError(safe_category(message.get("category")))
            if kind in {"STOPPED", "EOS"}:
                raise TransportError(TransportErrorCategory.WEBRTC_NEGOTIATION_FAILED)

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
            self.ice_state = str(message.get("ice_state", ""))[:32]
            self.connection_state = str(message.get("connection_state", ""))[:32]
            self.worker_dropped_samples = _int(message.get("dropped_samples", 0), 0, 2**31)
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
