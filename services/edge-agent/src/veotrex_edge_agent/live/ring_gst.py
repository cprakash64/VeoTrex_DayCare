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
WORKER_SCRIPT = (
    Path(__file__).resolve().parent.parent / "camera_transport" / "frame_worker.py"
)
READY_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 5.0
# One frame's worth of socket buffer. Anything larger turns the socket into the queue that
# newest-frame-wins exists to prevent.
SOCKET_BUFFER_BYTES = 256 * 1024


class GstFrameReader:
    """Decoded frames from a bounded appsink running in a system-Python worker process.

    ``source="synthetic"`` runs a local test pattern through the real depay/parse/decode/appsink
    route and is what the local qualification uses. ``source="appsrc"`` is the shape the WebRTC
    receive path will push RTP into once Ring access is restored; it is not exercised here,
    and this module does not pretend otherwise.
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
    ) -> None:
        self._source = source
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
        """Launch the worker. The session material is never passed to it.

        The worker decodes; it does not authorize. Keeping the handle on this side is what makes
        "the media process holds no credential" structural rather than a convention.
        """
        _ = material
        # A reconnect restarts this reader, and close() closed the previous slot for good.
        # Each session gets its own.
        self.slot = BoundedFrameSlot()
        self._eos = False
        if not self._python.exists():
            raise RingMediaError("WEBRTC_RUNTIME_UNAVAILABLE")
        if not WORKER_SCRIPT.is_file():
            raise RingMediaError("DECODER_START_FAILED")
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with contextlib.suppress(OSError):
            parent.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_BYTES)
            child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_BYTES)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script path
                [str(self._python), "-I", str(WORKER_SCRIPT), "--fd", str(child.fileno())],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            parent.close()
            child.close()
            raise RingMediaError("DECODER_START_FAILED") from None
        finally:
            child.close()
        self._sock = parent
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
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            message, _ = self._receive(0.5)
            if message is None:
                if self._process.poll() is not None:
                    raise RingMediaError("WORKER_EXITED")
                continue
            kind = message.get("type")
            if kind == "READY":
                self.negotiated = {"source": self._source, "decoder": self._decoder}
                return
            if kind == "ERROR":
                raise RingMediaError(str(message.get("category", "DECODER_START_FAILED")))
        raise RingMediaError("FIRST_MEDIA_TIMEOUT")

    def close(self) -> None:
        """Release worker, socket, slot and any mapping. Safe on a reader that never started."""
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.sendall(json.dumps({"type": "STOP"}).encode())
        process, self._process = self._process, None
        if process is not None:
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
            if kind == "ERROR":
                raise RingMediaError(str(message.get("category", "DECODER_FAILED")))

    def _materialise(
        self, header: dict[str, Any], descriptors: list[int]
    ) -> DecodedFrame | None:
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
            return np.frombuffer(mapping, dtype=np.uint8, count=expected).reshape(
                (height, width, 3)
            ).copy()

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

        ready, _, _ = select.select([sock], [], [], timeout)
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
