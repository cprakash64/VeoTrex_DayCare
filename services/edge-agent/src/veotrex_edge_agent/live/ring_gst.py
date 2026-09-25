"""Parent side of the decode worker: owns the process, maps frames, satisfies ``RingFrameReader``.

Pairs with ``camera_transport/frame_worker.py``. This half runs in the project venv - which has
NumPy and OpenCV but no PyGObject and an OpenCV built ``GStreamer: NO`` - so it never touches
GStreamer itself. It launches the worker under the system interpreter, receives one sealed
memfd per frame over an AF_UNIX socket, maps it, copies once into a NumPy array and closes the
descriptor.

The copy is deliberate. Handing the pipeline a view onto a mapping whose descriptor is about to
close would give the detector a frame that can vanish underneath it; one copy per published
frame at 8 fps is a few milliseconds and buys an owned, immutable array.

Nothing here writes a frame anywhere, and there is no path that could: the only frame storage is
an anonymous memfd that is unmapped and closed inside the same call that reads it.

``source="webrtc"`` (V1-DEMO-03C) is the real Ring path. It launches ``webrtc_worker.py`` in its
``frames`` egress mode instead of ``frame_worker.py``: the worker produces the recvonly offer, this
reader has the session material negotiate it (through the VeoTrex broker; the reader never sees
how), hands the answer back, and from then on receives exactly the same sealed-memfd FRAME
messages the synthetic route produces. One media stack, one frame transport.
"""

from __future__ import annotations

import array
import contextlib
import json
import mmap
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

from veotrex_edge_agent.camera_transport.errors import safe_category
from veotrex_edge_agent.camera_transport.frame_worker import (
    MAX_FRAME_BYTES,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    validate_sealed_frame,
)
from veotrex_edge_agent.live.ring_media import (
    BoundedFrameSlot,
    DecodedFrame,
    RingMediaError,
    RingSessionMaterial,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

SYSTEM_PYTHON = Path("/usr/bin/python3")
WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "camera_transport" / "frame_worker.py"
WEBRTC_WORKER_SCRIPT = (
    Path(__file__).resolve().parent.parent / "camera_transport" / "webrtc_worker.py"
)
READY_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 5.0
# A worker asked to STOP gets this long to tear its pipeline down before it is signalled.
GRACEFUL_STOP_SECONDS = 3.0
# Parent -> worker sends (ACK, STOP, ANSWER) never block longer than this on a wedged worker.
SEND_TIMEOUT_SECONDS = 2.0
SOURCE_MODES = frozenset({"synthetic", "appsrc", "webrtc"})
WEBRTC_PROTOCOL_VERSION = 1
HELLO_TIMEOUT_SECONDS = 20.0
# ICE gathering is bounded in the worker by GATHER_TIMEOUT_SECONDS; the offer wait adds margin.
GATHER_TIMEOUT_SECONDS = 15.0
OFFER_TIMEOUT_SECONDS = 30.0
MAX_SDP_BYTES = 61_440
WEBRTC_REQUIRED_ELEMENTS = ("webrtcbin", "nicesrc", "nicesink")
# The worker gets a fixed, minimal environment: nothing from the operator's shell reaches it.
_WORKER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def _category(value: object) -> str:
    """A worker-supplied category, mapped onto the fixed taxonomy; never echoed verbatim."""
    return safe_category(value).value


# One frame's worth of socket buffer. Anything larger turns the socket into the queue that
# newest-frame-wins exists to prevent.
SOCKET_BUFFER_BYTES = 256 * 1024


class GstFrameReader:
    """Decoded frames from a bounded appsink running in a system-Python worker process.

    ``source="synthetic"`` runs a local test pattern through the real depay/parse/decode/appsink
    route and is what the local qualification uses. ``source="webrtc"`` is the real receive
    route: WebRTC negotiated through the session material, then the same bounded appsink.
    ``source="appsrc"`` is an unfed placeholder kept for compatibility.

    No thread is ever started here: ``read`` runs on the caller's thread and ``close`` can be
    called from any other one, after which ``read`` fails fast rather than waiting out its
    timeout. A reader restarts only when ``start`` is called again explicitly.
    """

    def __init__(
        self,
        *,
        source: str = "synthetic",
        width: int = 640,
        height: int = 480,
        decoder: str = "nvidia",
        frames: int = 0,
        python_executable: Path = SYSTEM_PYTHON,
        stun_server: str | None = None,
    ) -> None:
        if source not in SOURCE_MODES:
            raise ValueError("unknown frame source")
        if decoder not in {"nvidia", "software"} and source == "webrtc":
            raise ValueError("the WebRTC frame route needs a decoder")
        self._source = source
        self._stun_server = stun_server
        self._generation = 0
        self._width = width
        self._height = height
        self._decoder = decoder
        self._frames = frames
        self._python = python_executable
        self._process: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self.slot = BoundedFrameSlot()
        self.negotiated: dict[str, Any] = {}
        self.frames_received = 0
        self.frames_dropped = 0
        self.decode_ms: list[float] = []
        self._eos = False
        self._unacknowledged = 0
        self._logger = structlog.get_logger()

    # --------------------------------------------------------------------------- lifecycle
    def start(self, material: RingSessionMaterial) -> None:
        """Launch the worker for one session. The session material is never passed to it.

        The worker decodes; it does not authorize. For WebRTC the only thing that crosses from
        the material to the worker is the SDP answer the material's negotiation returned, so
        "the media process holds no credential" stays structural rather than a convention.
        Any failure here leaves nothing running.
        """
        if self._process is not None:
            raise RingMediaError("reader_already_started")
        # A reconnect restarts this reader, and close() closed the previous slot for good.
        # Each session gets its own.
        self.slot = BoundedFrameSlot()
        self._eos = False
        self._unacknowledged = 0
        try:
            if self._source == "webrtc":
                self._start_webrtc(material)
            else:
                self._start_frame_worker()
        except BaseException:
            self.close()
            raise

    def _spawn(self, script: Path, *, minimal_env: bool) -> None:
        if not self._python.exists():
            raise RingMediaError("WEBRTC_RUNTIME_UNAVAILABLE")
        if script.is_symlink() or not script.is_file():
            raise RingMediaError("DECODER_START_FAILED")
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with contextlib.suppress(OSError):
            parent.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_BYTES)
            child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_BYTES)
        parent.settimeout(SEND_TIMEOUT_SECONDS)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script path
                [str(self._python), "-I", str(script), "--fd", str(child.fileno())],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(_WORKER_ENV) if minimal_env else None,
                start_new_session=minimal_env,
            )
        except OSError:
            parent.close()
            raise RingMediaError("DECODER_START_FAILED") from None
        finally:
            child.close()
        self._sock = parent

    def _start_frame_worker(self) -> None:
        self._spawn(WORKER_SCRIPT, minimal_env=False)
        self._send(
            {
                "type": "START",
                "protocol_version": PROTOCOL_VERSION,
                "source": self._source,
                "decoder": self._decoder,
                "width": self._width,
                "height": self._height,
                "frames": self._frames,
            }
        )
        self._await({"READY"}, READY_TIMEOUT_SECONDS, "FIRST_MEDIA_TIMEOUT")
        self.negotiated = {"source": self._source, "decoder": self._decoder}

    def _start_webrtc(self, material: RingSessionMaterial) -> None:
        negotiate = getattr(material, "negotiate", None)
        if not callable(negotiate):
            # A material that cannot negotiate is a wiring fault, never worth retrying.
            raise RingMediaError("ring_session_material_missing")
        self._spawn(WEBRTC_WORKER_SCRIPT, minimal_env=True)
        hello = self._await({"HELLO"}, HELLO_TIMEOUT_SECONDS, "SESSION_ACQUIRE_TIMEOUT")
        plugins = hello.get("plugins")
        required = WEBRTC_REQUIRED_ELEMENTS + (
            ("nvv4l2decoder",) if self._decoder == "nvidia" else ()
        )
        if not isinstance(plugins, dict) or not all(plugins.get(n) is True for n in required):
            raise RingMediaError("WEBRTC_RUNTIME_UNAVAILABLE")
        self._generation += 1
        self._send(
            {
                "type": "START",
                "protocol_version": WEBRTC_PROTOCOL_VERSION,
                "generation": self._generation,
                "codec": "H264",
                "decoder": self._decoder,
                "stun_server": self._stun_server,
                "gather_timeout_seconds": GATHER_TIMEOUT_SECONDS,
                "egress": "frames",
            }
        )
        offer = self._await({"OFFER"}, OFFER_TIMEOUT_SECONDS, "SESSION_ACQUIRE_TIMEOUT")
        sdp = offer.get("sdp")
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > MAX_SDP_BYTES:
            raise RingMediaError("WEBRTC_NEGOTIATION_FAILED")
        try:
            answer = negotiate(sdp)
        except RingMediaError:
            raise
        except Exception:
            # The negotiation's own error may carry a URL or a header: category only.
            raise RingMediaError("WHEP_OFFER_FAILED") from None
        if (
            not isinstance(answer, str)
            or not answer.startswith("v=0")
            or len(answer) > MAX_SDP_BYTES
        ):
            raise RingMediaError("WHEP_INVALID_ANSWER")
        self._send({"type": "ANSWER", "sdp": answer})
        self.negotiated = {"source": "webrtc", "decoder": self._decoder}

    def _await(self, kinds: set[str], timeout: float, timeout_category: str) -> dict[str, Any]:
        """Wait, bounded, for one control message; fail fast on any worker failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sock is None:
                raise RingMediaError("TRANSPORT_DISCONNECTED")
            message, descriptors = self._receive(min(0.5, max(0.0, deadline - time.monotonic())))
            for fd in descriptors:
                os.close(fd)
            if message is None:
                if self._process is not None and self._process.poll() is not None:
                    raise RingMediaError("WORKER_EXITED")
                continue
            kind = message.get("type")
            if kind in kinds:
                return message
            if kind in {"ERROR", "FAILED"}:
                raise RingMediaError(_category(message.get("category")))
            if kind in {"EOS", "STOPPED"}:
                raise RingMediaError("WEBRTC_NEGOTIATION_FAILED")
        raise RingMediaError(timeout_category)

    def close(self) -> None:
        """Release worker, socket, slot and any mapping. Safe on a reader that never started."""
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.sendall(json.dumps({"type": "STOP"}).encode())
        process, self._process = self._process, None
        if process is not None:
            # STOP first, so the worker takes its pipeline to NULL itself; a signal only when
            # it does not manage that in time.
            try:
                process.wait(timeout=GRACEFUL_STOP_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    process.terminate()
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
            if process.poll() is None:  # pragma: no cover - worker ignoring SIGTERM
                with contextlib.suppress(Exception):
                    process.kill()
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        self.slot.close()

    def __enter__(self) -> GstFrameReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------------- frame intake
    def read(self, timeout_seconds: float) -> DecodedFrame | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._sock is None or self.slot.closed:
                # close() ran, most likely from the scheduler stopping the source on another
                # thread. Returning at once is what lets the capture thread exit inside the
                # scheduler's join window instead of spinning here until the stall timeout.
                raise RingMediaError("TRANSPORT_DISCONNECTED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message, descriptors = self._receive(min(remaining, 0.5))
            if message is None:
                if self._process is not None and self._process.poll() is not None:
                    raise RingMediaError("WORKER_EXITED")
                continue
            kind = message.get("type")
            if kind == "FRAME":
                frame = self._materialise(message, descriptors)
                if frame is not None:
                    return frame
                continue
            for fd in descriptors:
                os.close(fd)
            if kind == "EOS":
                self._eos = True
                return None
            if kind in {"ERROR", "FAILED"}:
                raise RingMediaError(_category(message.get("category", "DECODER_FAILED")))
            if kind == "STOPPED":
                # The worker ended without EOS or a failure: the session is gone.
                raise RingMediaError("TRANSPORT_DISCONNECTED")
            # HEARTBEAT, CAPS, NEGOTIATED, CONNECTED: liveness chatter, not frames.

    def _materialise(self, header: dict[str, Any], descriptors: list[int]) -> DecodedFrame | None:
        if len(descriptors) != 1:
            for fd in descriptors:
                os.close(fd)
            self.frames_dropped += 1
            return None
        fd = descriptors[0]
        started = time.perf_counter_ns()
        try:
            width = int(header.get("width", 0))
            height = int(header.get("height", 0))
            expected = int(header.get("bytes", 0))
            if expected != width * height * 3 or not (0 < expected <= MAX_FRAME_BYTES):
                self.frames_dropped += 1
                return None
            # The worker sealed it; refusing anything else means a frame cannot be swapped or
            # resized between the send and the map.
            validate_sealed_frame(fd, expected)
            image = self._copy_frame(fd, expected, width, height)
        except Exception:
            self.frames_dropped += 1
            return None
        finally:
            os.close(fd)
            self._acknowledge()
        pts_ns = int(header.get("pts_ns", -1))
        frame = DecodedFrame(
            image=image,
            width=width,
            height=height,
            arrival_monotonic_ns=int(header.get("arrival_ns", time.monotonic_ns())),
            # -1 is the worker's "the decoder gave no presentation time". It stays None here
            # rather than becoming 0, which would read as the first instant of the stream.
            pts_ms=None if pts_ns < 0 else pts_ns / 1e6,
            discontinuity=bool(header.get("discontinuity", False)),
        )
        self.frames_received += 1
        self.decode_ms.append((time.perf_counter_ns() - started) / 1e6)
        if len(self.decode_ms) > 512:
            del self.decode_ms[: len(self.decode_ms) - 512]
        self.slot.publish(frame)
        taken = self.slot.take(0.0)
        return taken if taken is not None else frame

    @staticmethod
    def _copy_frame(fd: int, expected: int, width: int, height: int) -> NDArray[np.uint8]:
        with mmap.mmap(fd, expected, mmap.MAP_SHARED, mmap.PROT_READ) as mapping:
            # One copy, then the mapping goes away with the `with`. The pipeline gets an array
            # it owns, which cannot be unmapped underneath the detector.
            return (
                np.frombuffer(mapping, dtype=np.uint8, count=expected)
                .reshape((height, width, 3))
                .copy()
            )

    # ------------------------------------------------------------------------------ channel
    def _send(self, value: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise RingMediaError("DECODER_FAILED")
        with contextlib.suppress(OSError):
            sock.sendall(json.dumps(value, separators=(",", ":")).encode())

    def _acknowledge(self) -> None:
        self._unacknowledged += 1
        if self._unacknowledged >= 1:
            with contextlib.suppress(Exception):
                self._send({"type": "ACK", "count": self._unacknowledged})
            self._unacknowledged = 0

    def _receive(self, timeout: float) -> tuple[dict[str, Any] | None, list[int]]:
        sock = self._sock
        if sock is None:
            return (None, [])
        import select

        try:
            ready, _, _ = select.select([sock], [], [], timeout)
        except (OSError, ValueError):
            # close() ran on another thread and the descriptor is gone.
            return (None, [])
        if not ready:
            return (None, [])
        item_size = array.array("i").itemsize
        try:
            data, ancillary, flags, _ = sock.recvmsg(
                MAX_MESSAGE_BYTES + 1, socket.CMSG_SPACE(item_size)
            )
        except OSError:
            return (None, [])
        received: list[int] = []
        try:
            for level, kind, payload in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    raise RingMediaError("malformed_ancillary_data")
                descriptors = array.array("i")
                descriptors.frombytes(payload[: len(payload) - (len(payload) % item_size)])
                received.extend(descriptors)
            if flags & socket.MSG_CTRUNC:
                raise RingMediaError("ancillary_data_truncated")
        except Exception:
            for fd in received:
                os.close(fd)
            raise
        if not data:
            for fd in received:
                os.close(fd)
            return (None, [])
        try:
            message = json.loads(data.decode("utf-8", errors="strict"))
        except ValueError:
            for fd in received:
                os.close(fd)
            return (None, [])
        if not isinstance(message, dict):
            for fd in received:
                os.close(fd)
            return (None, [])
        return (message, received)

    # -------------------------------------------------------------------------------- stats
    @property
    def stats(self) -> dict[str, Any]:
        merged = dict(self.slot.stats.as_dict())
        merged.update(
            {
                "frames_received_total": self.frames_received,
                "frames_dropped_total": self.frames_dropped + self.slot.stats.dropped_total,
                "eos": self._eos,
            }
        )
        return merged

    @property
    def eos(self) -> bool:
        return self._eos

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None


def worker_available(python_executable: Path = SYSTEM_PYTHON) -> tuple[bool, str]:
    """Whether this host can actually run the decode worker. Read-only; installs nothing."""
    if not python_executable.exists():
        return (False, "system interpreter is absent")
    if not WORKER_SCRIPT.is_file():
        return (False, "decode worker script is absent")
    probe = subprocess.run(  # noqa: S603 - fixed interpreter and literal import probe
        [str(python_executable), "-I", "-c", "import gi; gi.require_version('Gst','1.0')"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return (False, "system interpreter has no PyGObject/GStreamer binding")
    return (True, "decode worker runtime present")


if __name__ == "__main__":  # pragma: no cover - manual probe
    available, detail = worker_available()
    print(f"decode worker runtime available={available}: {detail}")
    sys.exit(0 if available else 1)
