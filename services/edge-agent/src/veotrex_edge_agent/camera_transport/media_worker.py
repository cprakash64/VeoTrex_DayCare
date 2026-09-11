#!/usr/bin/python3
"""Standalone system-Python GStreamer camera transport worker (R5A).

Launched as ``/usr/bin/python3 -I media_worker.py --fd N``. The parent sends exactly one START
message over the inherited AF_UNIX/SOCK_SEQPACKET descriptor, so the transport credential never
appears in argv, the environment, a file, or a log. Pipeline topology is fixed in this file;
provider data only populates the re-validated rtspsrc ``location`` property. Media buffers are
never copied, retained, or written: probes record only timing metadata and sizes.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import select
import signal
import socket
import sys
import threading
import time
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
MAX_BATCH_SAMPLES = 400
MAX_PENDING_SAMPLES = 4_096
FLUSH_INTERVAL_SECONDS = 0.2
HEARTBEAT_INTERVAL_SECONDS = 1.0
START_TIMEOUT_SECONDS = 10.0
STOP_STATE_TIMEOUT_SECONDS = 3.0
CODEC_ROUTES = {"H264": ("rtph264depay", "h264parse"), "H265": ("rtph265depay", "h265parse")}
HARDWARE_DECODER = "nvv4l2decoder"
SOFTWARE_DECODERS = {"H264": ("openh264dec", "avdec_h264"), "H265": ("avdec_h265",)}
DECODER_MODES = frozenset({"nvidia", "software", "none"})
START_FIELDS = frozenset(
    {
        "type",
        "protocol_version",
        "generation",
        "location",
        "user_id",
        "password",
        "latency_ms",
        "tcp_timeout_ms",
        "decoder",
    }
)
CLOCK_TIME_NONE = 2**64 - 1


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG: never outlive the edge agent.


class Channel:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False

    def send(self, value: dict[str, Any]) -> None:
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("message_too_large")
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.send(data)
            except OSError:
                self.closed = True

    def receive(self, timeout: float) -> dict[str, Any] | None:
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        data = self.sock.recv(MAX_MESSAGE_BYTES + 1)
        if not data:
            raise EOFError
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("message_too_large")
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise ValueError("invalid_envelope")
        return value


def origin_of(uri: str) -> tuple[str, str] | None:
    lowered = uri.lower()
    for scheme in ("rtsps://", "rtsp://"):
        if lowered.startswith(scheme):
            authority = lowered[len(scheme) :].split("/", 1)[0].split("?", 1)[0]
            if not authority or "@" in authority:
                return None
            return scheme, authority
    return None


def valid_location(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 2048:
        return False
    if any(not 33 <= ord(character) <= 126 for character in value) or "#" in value:
        return False
    return origin_of(value) is not None


def validate_start(message: dict[str, Any]) -> dict[str, Any]:
    if set(message) != START_FIELDS or message.get("type") != "START":
        raise ValueError("invalid_start")
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("protocol_version_mismatch")
    generation = message.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("invalid_generation")
    if not valid_location(message.get("location")):
        raise ValueError("invalid_location")
    for key in ("user_id", "password"):
        if not isinstance(message.get(key), str) or len(message[key]) > 16_384:
            raise ValueError("invalid_credential_field")
    latency = message.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, int) or not 0 <= latency <= 5000:
        raise ValueError("invalid_latency")
    tcp_timeout = message.get("tcp_timeout_ms")
    if (
        isinstance(tcp_timeout, bool)
        or not isinstance(tcp_timeout, int)
        or not 1000 <= tcp_timeout <= 60_000
    ):
        raise ValueError("invalid_tcp_timeout")
    if message.get("decoder") not in DECODER_MODES:
        raise ValueError("invalid_decoder_mode")
    return message


def load_gstreamer() -> tuple[Any, Any, Any]:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtsp", "1.0")
    from gi.repository import GLib, Gst, GstRtsp  # type: ignore[import-not-found]

    Gst.init(None)
    return Gst, GstRtsp, GLib


def classify_error(
    gst: Any, error: Any, debug: str | None, role: str, *, connected: bool, decoded: bool
) -> str:
    """Map a GStreamer error onto the taxonomy. Error text is inspected, never forwarded."""
    if role == "decoder":
        return "DECODER_FAILED" if decoded else "DECODER_START_FAILED"
    if role in {"depayloader", "parser"}:
        return "INTERNAL_TRANSPORT_ERROR" if decoded else "CODEC_UNSUPPORTED"
    domain = str(getattr(error, "domain", ""))
    code = int(getattr(error, "code", -1))
    text = f"{getattr(error, 'message', '') or ''} {debug or ''}".lower()
    resource = domain == "gst-resource-error-quark"
    if resource and code == int(gst.ResourceError.NOT_AUTHORIZED):
        return "AUTHORIZATION_FAILED"
    if "401" in text or "unauthorized" in text:
        return "AUTHORIZATION_FAILED"
    if "tls" in text or "certificate" in text:
        return "TRANSPORT_CONNECT_FAILED"
    if "461" in text or "unsupported transport" in text:
        return "TRANSPORT_PROTOCOL_UNSUPPORTED"
    if (resource and code == int(gst.ResourceError.NOT_FOUND)) or "404" in text:
        return "CAMERA_OFFLINE"
    if resource or "could not" in text or "timeout" in text:
        return "TRANSPORT_DISCONNECTED" if connected else "TRANSPORT_CONNECT_FAILED"
    if domain == "gst-stream-error-quark":
        return "DECODER_FAILED" if decoded else "CODEC_UNSUPPORTED"
    return "INTERNAL_TRANSPORT_ERROR"


class TransportSession:
    def __init__(self, channel: Channel, start: dict[str, Any]) -> None:
        self.channel = channel
        self.generation = int(start["generation"])
        self.location = str(start["location"])
        self.origin = origin_of(self.location)
        self.decoder_mode = str(start["decoder"])
        self.lock = threading.Lock()
        self.compressed: list[list[int]] = []
        self.decoded: list[list[int]] = []
        self.dropped_samples = 0
        self.decoded_total = 0
        self.connected = False
        self.redirect_refused = False
        self.failure: str | None = None
        self.caps_report: dict[str, Any] | None = None
        self.caps_sent = False
        self.roles: dict[str, str] = {"source": "source"}
        self.warnings = 0
        self.gst, self.gst_rtsp, _glib = load_gstreamer()
        self.pipeline = self.gst.Pipeline.new(f"veotrex-transport-{self.generation}")
        source = self.gst.ElementFactory.make("rtspsrc", "source")
        if self.pipeline is None or source is None:
            raise RuntimeError("DECODER_START_FAILED")
        self.source = source
        source.set_property("location", self.location)
        source.set_property("protocols", self.gst_rtsp.RTSPLowerTrans.TCP)
        source.set_property("latency", int(start["latency_ms"]))
        source.set_property("tcp-timeout", int(start["tcp_timeout_ms"]) * 1000)
        if start["user_id"]:
            source.set_property("user-id", start["user_id"])
            source.set_property("user-pw", start["password"])
        start["password"] = ""
        source.connect("before-send", self._before_send)
        source.connect("select-stream", self._select_stream)
        source.connect("on-sdp", self._on_sdp)
        source.connect("pad-added", self._on_pad_added)
        self.pipeline.add(source)

    # --- rtspsrc signal handlers (streaming threads) ---------------------------------
    def _before_send(self, _source: Any, message: Any) -> bool:
        """Refuse redirects and cross-origin control URLs before any request is sent."""
        try:
            result, _method, uri, _version = message.parse_request()
        except (TypeError, ValueError):
            return True
        if result != self.gst_rtsp.RTSPResult.OK or uri in (None, "*"):
            return True
        if origin_of(str(uri)) != self.origin:
            self.redirect_refused = True
            return False
        return True

    def _select_stream(self, _source: Any, _index: int, caps: Any) -> bool:
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        return structure is not None and structure.get_string("media") == "video"

    def _on_sdp(self, _source: Any, _sdp: Any) -> None:
        if not self.connected:
            self.connected = True
            self.channel.send(
                {"type": "CONNECTED", "generation": self.generation, "at_ns": time.monotonic_ns()}
            )

    def _fail(self, category: str) -> None:
        if self.failure is None:
            self.failure = category

    def _on_pad_added(self, _source: Any, pad: Any) -> None:
        gst = self.gst
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if structure is None or structure.get_string("media") != "video":
            return
        encoding = (structure.get_string("encoding-name") or "").upper().replace(".", "")
        encoding = {"AVC": "H264", "HEVC": "H265"}.get(encoding, encoding)
        route = CODEC_ROUTES.get(encoding)
        if route is None:
            self._fail("CODEC_UNSUPPORTED")
            return
        names = [route[0], route[1]]
        decoder_name: str | None = None
        if self.decoder_mode == "nvidia":
            decoder_name = HARDWARE_DECODER if gst.ElementFactory.find(HARDWARE_DECODER) else None
        elif self.decoder_mode == "software":
            decoder_name = next(
                (name for name in SOFTWARE_DECODERS[encoding] if gst.ElementFactory.find(name)),
                None,
            )
        if self.decoder_mode != "none" and decoder_name is None:
            self._fail("DECODER_START_FAILED")
            return
        if decoder_name:
            names.append(decoder_name)
        names.append("fakesink")
        roles = ["depayloader", "parser"] + (["decoder"] if decoder_name else []) + ["sink"]
        elements = []
        for index, (name, role) in enumerate(zip(names, roles, strict=True)):
            element = gst.ElementFactory.make(name, f"{role}-{index}")
            if element is None:
                self._fail("DECODER_START_FAILED" if role == "decoder" else "CODEC_UNSUPPORTED")
                return
            self.roles[element.get_name()] = role
            elements.append(element)
        sink = elements[-1]
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("enable-last-sample", False)  # Never retain a decoded frame.
        for element in elements:
            self.pipeline.add(element)
        for index in range(len(elements) - 1):
            if not elements[index].link(elements[index + 1]):
                self._fail("INTERNAL_TRANSPORT_ERROR")
                return
        for element in elements:
            element.sync_state_with_parent()
        if pad.link(elements[0].get_static_pad("sink")) != gst.PadLinkReturn.OK:
            self._fail("INTERNAL_TRANSPORT_ERROR")
            return
        elements[1].get_static_pad("src").add_probe(gst.PadProbeType.BUFFER, self._compressed_probe)
        if decoder_name:
            sink.get_static_pad("sink").add_probe(gst.PadProbeType.BUFFER, self._decoded_probe)
        self.channel.send(
            {
                "type": "NEGOTIATED",
                "generation": self.generation,
                "at_ns": time.monotonic_ns(),
                "codec": encoding,
                "decoder": decoder_name,
                "hardware_decoder": decoder_name == HARDWARE_DECODER,
            }
        )

    @staticmethod
    def _ts(value: int) -> int:
        return -1 if value == CLOCK_TIME_NONE else int(value)

    def _append(self, target: list[list[int]], sample: list[int]) -> None:
        with self.lock:
            if len(self.compressed) + len(self.decoded) >= MAX_PENDING_SAMPLES:
                self.dropped_samples += 1
                return
            target.append(sample)

    def _compressed_probe(self, _pad: Any, info: Any) -> Any:
        buffer = info.get_buffer()
        if buffer is not None:
            self._append(
                self.compressed,
                [
                    time.monotonic_ns(),
                    self._ts(buffer.pts),
                    self._ts(buffer.dts),
                    buffer.get_size(),
                ],
            )
        return self.gst.PadProbeReturn.OK

    def _decoded_probe(self, pad: Any, info: Any) -> Any:
        buffer = info.get_buffer()
        if buffer is not None:
            self._append(self.decoded, [time.monotonic_ns(), self._ts(buffer.pts)])
            self.decoded_total += 1
            if self.caps_report is None:
                caps = pad.get_current_caps()
                structure = caps.get_structure(0) if caps and caps.get_size() else None
                features = caps.get_features(0) if caps and caps.get_size() else None
                report: dict[str, Any] = {"width": None, "height": None, "framerate": None}
                if structure is not None:
                    ok, width = structure.get_int("width")
                    report["width"] = width if ok else None
                    ok, height = structure.get_int("height")
                    report["height"] = height if ok else None
                    ok, numerator, denominator = structure.get_fraction("framerate")
                    if ok and denominator:
                        report["framerate"] = numerator / denominator
                report["nvmm"] = bool(features and features.contains("memory:NVMM"))
                self.caps_report = report
        return self.gst.PadProbeReturn.OK

    # --- main loop -------------------------------------------------------------------
    def flush(self) -> None:
        while True:
            with self.lock:
                compressed = self.compressed[:MAX_BATCH_SAMPLES]
                decoded = self.decoded[:MAX_BATCH_SAMPLES]
                del self.compressed[: len(compressed)]
                del self.decoded[: len(decoded)]
            if not compressed and not decoded:
                return
            self.channel.send(
                {
                    "type": "MEDIA",
                    "generation": self.generation,
                    "compressed": compressed,
                    "decoded": decoded,
                }
            )

    def run(self) -> str:
        gst = self.gst
        if self.pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            return "INTERNAL_TRANSPORT_ERROR"
        bus = self.pipeline.get_bus()
        mask = gst.MessageType.ERROR | gst.MessageType.EOS | gst.MessageType.WARNING
        last_flush = last_heartbeat = time.monotonic()
        outcome = "STOPPED"
        while True:
            message = bus.timed_pop_filtered(50 * gst.MSECOND, mask)
            if self.redirect_refused:
                outcome = "REDIRECT_REFUSED"
                break
            if self.failure is not None:
                outcome = self.failure
                break
            if message is not None:
                if message.type == gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    source = message.src.get_name() if message.src else ""
                    outcome = classify_error(
                        gst,
                        error,
                        debug,
                        self.roles.get(source, "unknown"),
                        connected=self.connected,
                        decoded=self.decoded_total > 0,
                    )
                    if self.redirect_refused:
                        outcome = "REDIRECT_REFUSED"
                    break
                if message.type == gst.MessageType.EOS:
                    outcome = "EOS"
                    break
                self.warnings += 1
            try:
                command = self.channel.receive(0)
            except (EOFError, OSError, ValueError):
                outcome = "PARENT_GONE"
                break
            if command is not None and command.get("type") == "STOP":
                outcome = "STOPPED"
                break
            now = time.monotonic()
            if self.caps_report is not None and not self.caps_sent:
                self.caps_sent = True
                self.channel.send(
                    {
                        "type": "CAPS",
                        "generation": self.generation,
                        "at_ns": time.monotonic_ns(),
                        **self.caps_report,
                    }
                )
            if now - last_flush >= FLUSH_INTERVAL_SECONDS:
                self.flush()
                last_flush = now
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                self.channel.send(
                    {
                        "type": "HEARTBEAT",
                        "generation": self.generation,
                        "at_ns": time.monotonic_ns(),
                        "dropped_samples": self.dropped_samples,
                        "warnings": self.warnings,
                    }
                )
                last_heartbeat = now
            if self.channel.closed:
                outcome = "PARENT_GONE"
                break
        self.flush()
        return outcome

    def close(self) -> None:
        self.pipeline.set_state(self.gst.State.NULL)
        self.pipeline.get_state(int(STOP_STATE_TIMEOUT_SECONDS * self.gst.SECOND))


def main() -> int:
    _die_with_parent()
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    arguments = parser.parse_args()
    sock = socket.socket(fileno=arguments.fd)
    channel = Channel(sock)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    try:
        try:
            gst, _rtsp, _glib = load_gstreamer()
            plugins = {
                name: gst.ElementFactory.find(name) is not None
                for name in (
                    "rtspsrc",
                    "rtph264depay",
                    "rtph265depay",
                    "h264parse",
                    "h265parse",
                    HARDWARE_DECODER,
                )
            }
            channel.send(
                {
                    "type": "HELLO",
                    "protocol_version": PROTOCOL_VERSION,
                    "worker_pid": os.getpid(),
                    "gstreamer_version": gst.version_string(),
                    "plugins": plugins,
                }
            )
        except (ImportError, ValueError):
            channel.send(
                {"type": "HELLO", "protocol_version": PROTOCOL_VERSION, "worker_pid": os.getpid()}
            )
            channel.send({"type": "FAILED", "generation": 0, "category": "DECODER_START_FAILED"})
            return 3
        try:
            start = channel.receive(START_TIMEOUT_SECONDS)
            if start is None:
                return 4
            start = validate_start(start)
        except (EOFError, OSError, ValueError, UnicodeError):
            channel.send(
                {"type": "FAILED", "generation": 0, "category": "INTERNAL_TRANSPORT_ERROR"}
            )
            return 4
        generation = int(start["generation"])
        try:
            session = TransportSession(channel, start)
        except RuntimeError as exc:
            category = (
                str(exc) if str(exc) == "DECODER_START_FAILED" else "INTERNAL_TRANSPORT_ERROR"
            )
            channel.send({"type": "FAILED", "generation": generation, "category": category})
            return 5
        finally:
            start.clear()
        try:
            outcome = session.run()
        finally:
            session.close()
        at = time.monotonic_ns()
        if outcome == "EOS":
            channel.send({"type": "EOS", "generation": generation, "at_ns": at})
        elif outcome not in {"STOPPED", "PARENT_GONE"}:
            channel.send(
                {"type": "FAILED", "generation": generation, "category": outcome, "at_ns": at}
            )
        channel.send({"type": "STOPPED", "generation": generation, "at_ns": time.monotonic_ns()})
        return 0
    finally:
        with contextlib.suppress(OSError):
            sock.close()


if __name__ == "__main__":
    sys.exit(main())
