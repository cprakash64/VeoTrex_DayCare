#!/usr/bin/python3
"""Standalone system-Python decode worker that lets decoded frames LEAVE the media subsystem.

Launched as ``/usr/bin/python3 -I frame_worker.py --fd N``.

This is the capability V1-DEMO-01's transport did not have. ``webrtc_worker.py`` terminates its
receive route in ``fakesink`` and reports only timing metadata over its control socket, which is
correct for qualifying a transport and useless for feeding a detector. This worker ends the same
route in ``appsink`` instead and ships the pixels to the parent.

Why a separate process at all: the project venv's OpenCV is built ``GStreamer: NO`` and the venv
has no PyGObject, while the system Python does. The existing transport already solved this with
a system-Python worker, so this follows that shape rather than inventing a second one.

**Frames move by sealed memfd, not through the socket.** A 1280x720 BGR frame is 2.7 MB and the
control socket's messages are capped at 64 KiB; more importantly a socket buffer *is* a queue,
and a queue is what newest-frame-wins exists to avoid. Each published frame is written to an
anonymous, sealed, read-only memfd whose descriptor travels as SCM_RIGHTS beside a small JSON
header. The parent maps it, copies once, and closes it. Nothing is ever written to a path, so
"no frame persistence" is a property of the mechanism rather than a promise.

Bounded in three places, deliberately:
  appsink       ``max-buffers=1 drop=true`` - GStreamer discards upstream rather than growing
  send          non-blocking; a send that would block means the parent is behind, so the frame
                is dropped and counted instead of queueing behind it
  in flight     at most ``MAX_FRAMES_IN_FLIGHT`` unacknowledged frames

Credential-free by construction: no token, no SDP and no endpoint is accepted or emitted here.
The pipeline is either a local synthetic producer or a receive route fed by the existing
WebRTC worker, and in neither case does this process learn who the camera belongs to.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import ctypes
import fcntl
import json
import os
import select
import signal
import socket
import stat
import sys
import threading
import time
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
MAX_FRAMES_IN_FLIGHT = 2
# Bounds the memfd a malformed caps negotiation could ask for: 4096x4096 BGR.
MAX_FRAME_BYTES = 4096 * 4096 * 3
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
CLOCK_TIME_NONE = 2**64 - 1
START_TIMEOUT_SECONDS = 10.0
SOURCE_MODES = frozenset({"synthetic", "appsrc"})
DECODER_MODES = frozenset({"nvidia", "software", "none"})
HARDWARE_DECODER = "nvv4l2decoder"
SOFTWARE_DECODERS = ("openh264dec", "avdec_h264")


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


def create_sealed_frame_memfd(payload: memoryview) -> int:
    """An anonymous, immutable, read-only-by-seal buffer holding exactly one frame.

    Sealed for the same reason the GPU worker seals its tensors: once the descriptor has been
    handed over, neither side can resize or rewrite the mapping under the other.
    """
    fd = os.memfd_create("veotrex-frame", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.ftruncate(fd, len(payload))
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("frame_write_failed")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
        return fd
    except Exception:
        os.close(fd)
        raise


def validate_sealed_frame(fd: int, expected_bytes: int) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_bytes:
        raise OSError("invalid_frame_size")
    if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS != SEALS:
        raise OSError("mutable_frame_rejected")


class Channel:
    """JSON control messages, with an optional descriptor riding alongside one of them."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False

    def send(self, value: dict[str, Any], fd: int | None = None) -> bool:
        """True when the message was handed to the kernel, False when the peer is behind.

        A full socket buffer is the signal that the parent has not kept up. Returning False so
        the caller can drop the frame is the whole backpressure policy: blocking here would
        stall the decoder, and queueing would defeat newest-frame-wins.
        """
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        if len(data) > MAX_MESSAGE_BYTES:
            return False
        with self._lock:
            if self.closed:
                return False
            try:
                if fd is None:
                    self.sock.sendall(data)
                    return True
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
                self.sock.sendmsg([data], ancillary)
                return True
            except BlockingIOError:
                return False
            except OSError:
                self.closed = True
                return False

    def receive(self, timeout: float) -> dict[str, Any] | None:
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        data = self.sock.recv(MAX_MESSAGE_BYTES + 1)
        if not data:
            raise EOFError
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise ValueError("invalid_envelope")
        return value


def validate_start(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("type") != "START" or message.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("invalid_start")
    source = message.get("source")
    if source not in SOURCE_MODES:
        raise ValueError("invalid_source")
    decoder = message.get("decoder", "nvidia")
    if decoder not in DECODER_MODES:
        raise ValueError("invalid_decoder")
    width = int(message.get("width", 640))
    height = int(message.get("height", 480))
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
        raise ValueError("invalid_geometry")
    return {
        "source": source,
        "decoder": decoder,
        "width": width,
        "height": height,
        "frames": int(message.get("frames", 0)),
    }


class FrameWorker:
    """One GStreamer pipeline ending in a bounded appsink, publishing frames to the parent."""

    def __init__(self, channel: Channel, options: dict[str, Any]) -> None:
        self.channel = channel
        self.options = options
        self.gst: Any = None
        self.pipeline: Any = None
        self.sequence = 0
        self.published = 0
        self.dropped = 0
        self.stopping = threading.Event()
        self.in_flight = 0
        self._lock = threading.Lock()
        self.discontinuity = True  # the first frame of any session starts a new timeline

    # --------------------------------------------------------------------------- pipeline
    def _description(self) -> str:
        """The receive route, with a producer in front of it.

        ``synthetic`` encodes a local test pattern and decodes it again through exactly the
        depay/parse/decode chain a WHEP receive route uses. That is what makes a local
        qualification meaningful: the decode and egress half is the real one, and only the
        WebRTC front end is replaced.
        """
        width, height = self.options["width"], self.options["height"]
        decoder = self._decoder_element()
        tail = (
            f"{decoder} ! videoconvert ! video/x-raw,format=BGR ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        if self.options["source"] == "synthetic":
            return (
                f"videotestsrc is-live=true pattern=ball ! "
                f"video/x-raw,width={width},height={height},framerate=15/1 ! "
                "videoconvert ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=15 ! "
                "video/x-h264,profile=baseline ! rtph264pay ! rtph264depay ! h264parse ! " + tail
            )
        # appsrc: RTP payloads pushed in by the WebRTC receive path.
        return (
            "appsrc name=src is-live=true do-timestamp=true format=time "
            "caps=application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 ! "
            "rtph264depay ! h264parse ! " + tail
        )

    def _decoder_element(self) -> str:
        mode = self.options["decoder"]
        if mode == "none":
            return "identity"
        factory = self.gst.ElementFactory
        if mode == "nvidia" and factory.find(HARDWARE_DECODER) is not None:
            # nvv4l2decoder outputs NVMM memory; nvvidconv brings it back to system memory,
            # which is the only place appsink can hand it to the parent.
            #
            # The NV12 caps after nvvidconv are load-bearing on this Jetson, not decoration.
            # Left to negotiate freely the element picks a transform the VIC refuses, and the
            # pipeline dies at the first buffer with "NvBufSurfTransform Failed" - measured,
            # not guessed.
            return f"{HARDWARE_DECODER} ! nvvidconv ! video/x-raw,format=NV12"
        for name in SOFTWARE_DECODERS:
            if factory.find(name) is not None:
                return name
        raise RuntimeError("DECODER_START_FAILED")

    def build(self) -> None:
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst  # type: ignore[import-not-found]

        Gst.init(None)
        self.gst = Gst
        description = self._description()
        self.pipeline = Gst.parse_launch(description)
        sink = self.pipeline.get_by_name("sink")
        if sink is None:
            raise RuntimeError("DECODER_START_FAILED")
        sink.connect("new-sample", self._on_sample)

    # ------------------------------------------------------------------------ frame egress
    def _on_sample(self, sink: Any) -> Any:
        sample = sink.emit("pull-sample")
        if sample is None or self.stopping.is_set():
            return self.gst.FlowReturn.OK
        with self._lock:
            if self.in_flight >= MAX_FRAMES_IN_FLIGHT:
                # The parent has not collected what it already has. Newest-frame-wins means
                # this one is discarded now rather than waiting behind the others.
                self.dropped += 1
                return self.gst.FlowReturn.OK
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if buffer is None or structure is None:
            return self.gst.FlowReturn.OK
        width = int(structure.get_value("width") or 0)
        height = int(structure.get_value("height") or 0)
        expected = width * height * 3
        if not (0 < expected <= MAX_FRAME_BYTES):
            return self.gst.FlowReturn.OK
        ok, mapped = buffer.map(self.gst.MapFlags.READ)
        if not ok:
            return self.gst.FlowReturn.OK
        started = time.perf_counter_ns()
        try:
            payload = memoryview(mapped.data)[:expected]
            if len(payload) < expected:
                return self.gst.FlowReturn.OK
            fd = create_sealed_frame_memfd(payload)
        except Exception:
            self.dropped += 1
            return self.gst.FlowReturn.OK
        finally:
            buffer.unmap(mapped)
        try:
            self.sequence += 1
            header = {
                "type": "FRAME",
                "sequence": self.sequence,
                "width": width,
                "height": height,
                "bytes": expected,
                "pts_ns": -1 if buffer.pts == CLOCK_TIME_NONE else int(buffer.pts),
                "arrival_ns": time.monotonic_ns(),
                "encode_ms": (time.perf_counter_ns() - started) / 1e6,
                "discontinuity": self.discontinuity,
            }
            if self.channel.send(header, fd):
                self.discontinuity = False
                self.published += 1
                with self._lock:
                    self.in_flight += 1
            else:
                self.dropped += 1
        finally:
            # The descriptor is duplicated into the parent by SCM_RIGHTS; this side always
            # closes its own copy, sent or not, or the worker leaks a memfd per frame.
            os.close(fd)
        return self.gst.FlowReturn.OK

    def acknowledge(self, count: int = 1) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - count)

    # --------------------------------------------------------------------------- lifecycle
    def run(self) -> None:
        gst = self.gst
        if self.pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            self.channel.send({"type": "ERROR", "category": "DECODER_START_FAILED"})
            return
        self.channel.send({"type": "READY", "protocol_version": PROTOCOL_VERSION})
        bus = self.pipeline.get_bus()
        limit = self.options["frames"]
        while not self.stopping.is_set():
            message = bus.timed_pop_filtered(
                50 * gst.MSECOND, gst.MessageType.ERROR | gst.MessageType.EOS
            )
            if message is not None:
                if message.type == gst.MessageType.EOS:
                    self.channel.send({"type": "EOS"})
                else:
                    # The GStreamer message text can name a device or a file; only a bounded
                    # category leaves this process.
                    self.channel.send({"type": "ERROR", "category": "DECODER_FAILED"})
                break
            with contextlib.suppress(Exception):
                control = self.channel.receive(0.0)
                if control is not None:
                    kind = control.get("type")
                    if kind == "ACK":
                        self.acknowledge(int(control.get("count", 1)))
                    elif kind == "STOP":
                        break
            if limit and self.published >= limit:
                self.channel.send({"type": "EOS"})
                break

    def stop(self) -> None:
        self.stopping.set()
        if self.pipeline is not None and self.gst is not None:
            self.pipeline.set_state(self.gst.State.NULL)
            self.pipeline.get_state(3 * self.gst.SECOND)
            self.pipeline = None


def main(argv: list[str] | None = None) -> int:
    _die_with_parent()
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    arguments = parser.parse_args(argv)
    sock = socket.socket(fileno=arguments.fd)
    sock.setblocking(False)
    channel = Channel(sock)
    worker: FrameWorker | None = None
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        message = None
        while message is None and time.monotonic() < deadline:
            message = channel.receive(0.2)
        if message is None:
            channel.send({"type": "ERROR", "category": "SESSION_ACQUIRE_TIMEOUT"})
            return 1
        options = validate_start(message)
        worker = FrameWorker(channel, options)
        worker.build()
        worker.run()
        return 0
    except Exception as exc:
        category = "DECODER_START_FAILED" if isinstance(exc, RuntimeError) else "DECODER_FAILED"
        channel.send({"type": "ERROR", "category": category})
        return 1
    finally:
        if worker is not None:
            worker.stop()
        with contextlib.suppress(OSError):
            sock.close()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
