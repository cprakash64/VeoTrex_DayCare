#!/usr/bin/python3
"""Standalone system-Python WebRTC receive worker (R5A-R2).

Launched as ``/usr/bin/python3 -I webrtc_worker.py --fd N``. It owns one recvonly, video-only
``webrtcbin`` peer and the fixed receive route
``webrtcbin -> depay -> parse -> nvv4l2decoder -> fakesink``.

Deliberately credential-free: the WHEP Bearer token never enters this process. The worker emits a
complete (non-trickle) SDP offer to the parent, the parent performs the authenticated exchange,
and the answer comes back down the same inherited AF_UNIX/SOCK_SEQPACKET socket. Media buffers are
never copied, retained, or written; probes record timing metadata and sizes only.
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
MAX_SDP_BYTES = 61_440
MAX_BATCH_SAMPLES = 400
MAX_PENDING_SAMPLES = 4_096
FLUSH_INTERVAL_SECONDS = 0.2
HEARTBEAT_INTERVAL_SECONDS = 1.0
START_TIMEOUT_SECONDS = 10.0
ANSWER_TIMEOUT_SECONDS = 30.0
CODEC_ROUTES = {"H264": ("rtph264depay", "h264parse"), "H265": ("rtph265depay", "h265parse")}
HARDWARE_DECODER = "nvv4l2decoder"
DECODER_MODES = frozenset({"nvidia", "software", "none"})
SOFTWARE_DECODERS = {"H264": ("openh264dec", "avdec_h264"), "H265": ("avdec_h265",)}
START_FIELDS = frozenset(
    {
        "type",
        "protocol_version",
        "generation",
        "codec",
        "decoder",
        "stun_server",
        "gather_timeout_seconds",
    }
)
CLOCK_TIME_NONE = 2**64 - 1


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


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


def validate_start(message: dict[str, Any]) -> dict[str, Any]:
    if set(message) != START_FIELDS or message.get("type") != "START":
        raise ValueError("invalid_start")
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("protocol_version_mismatch")
    generation = message.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("invalid_generation")
    if message.get("codec") not in CODEC_ROUTES:
        raise ValueError("invalid_codec")
    if message.get("decoder") not in DECODER_MODES:
        raise ValueError("invalid_decoder_mode")
    stun = message.get("stun_server")
    if stun is not None and (
        not isinstance(stun, str)
        or not stun.startswith("stun://")
        or len(stun) > 512
        or any(not 32 < ord(c) < 127 for c in stun)
    ):
        raise ValueError("invalid_stun_server")
    timeout = message.get("gather_timeout_seconds")
    if not isinstance(timeout, int | float) or not 1 <= timeout <= 60:
        raise ValueError("invalid_gather_timeout")
    return message


def validate_sdp(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SDP_BYTES:
        raise ValueError("invalid_sdp")
    if not value.startswith("v=0"):
        raise ValueError("invalid_sdp")
    return value


def load_gstreamer() -> tuple[Any, Any, Any, Any]:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # type: ignore[import-not-found]

    Gst.init(None)
    return Gst, GstWebRTC, GstSdp, GLib


class WebRtcReceiver:
    def __init__(self, channel: Channel, start: dict[str, Any]) -> None:
        self.channel = channel
        self.generation = int(start["generation"])
        self.codec = str(start["codec"])
        self.decoder_mode = str(start["decoder"])
        self.gather_timeout = float(start["gather_timeout_seconds"])
        self.gst, self.webrtc, self.sdp_module, self.glib = load_gstreamer()
        self.lock = threading.Lock()
        self.compressed: list[list[int]] = []
        self.decoded: list[list[int]] = []
        self.dropped_samples = 0
        self.decoded_total = 0
        self.failure: str | None = None
        self.offer_sent = False
        self.answer_applied = False
        self.caps_report: dict[str, Any] | None = None
        self.caps_sent = False
        self.connected_sent = False
        self.ice_state = ""
        self.connection_state = ""
        self.negotiated_sent = False
        self.pipeline = self.gst.Pipeline.new(f"veotrex-webrtc-{self.generation}")
        peer = self.gst.ElementFactory.make("webrtcbin", "peer")
        if self.pipeline is None or peer is None:
            raise RuntimeError("WEBRTC_RUNTIME_UNAVAILABLE")
        self.peer = peer
        peer.set_property("bundle-policy", "max-bundle")
        if start.get("stun_server"):
            peer.set_property("stun-server", start["stun_server"])
        self.pipeline.add(peer)
        caps = self.gst.Caps.from_string(
            f"application/x-rtp,media=video,encoding-name={self.codec},clock-rate=90000,payload=96"
        )
        # Video-only, receive-only: no audio transceiver is ever added.
        peer.emit("add-transceiver", self.webrtc.WebRTCRTPTransceiverDirection.RECVONLY, caps)
        peer.connect("pad-added", self._on_pad_added)
        peer.connect("notify::ice-gathering-state", self._on_ice_gathering)
        peer.connect("notify::ice-connection-state", self._on_ice_connection)
        peer.connect("notify::connection-state", self._on_connection)

    # --- signals ------------------------------------------------------------------
    def _fail(self, category: str) -> None:
        if self.failure is None:
            self.failure = category

    def _on_ice_gathering(self, *_: Any) -> None:
        state = self.peer.get_property("ice-gathering-state")
        self.ice_state = state.value_nick
        if state == self.webrtc.WebRTCICEGatheringState.COMPLETE and not self.offer_sent:
            self._send_complete_offer()

    def _on_ice_connection(self, *_: Any) -> None:
        state = self.peer.get_property("ice-connection-state")
        nick = state.value_nick
        if nick == "failed":
            self._fail("WEBRTC_ICE_FAILED")

    def _on_connection(self, *_: Any) -> None:
        state = self.peer.get_property("connection-state")
        self.connection_state = state.value_nick
        if self.connection_state == "failed":
            self._fail("WEBRTC_CONNECTION_FAILED")
        elif self.connection_state == "connected" and not self.connected_sent:
            self.connected_sent = True
            self.channel.send(
                {"type": "CONNECTED", "generation": self.generation, "at_ns": time.monotonic_ns()}
            )

    def _send_complete_offer(self) -> None:
        description = self.peer.get_property("local-description")
        if description is None or description.sdp is None:
            self._fail("WEBRTC_NEGOTIATION_FAILED")
            return
        text = description.sdp.as_text()
        if not text or len(text.encode()) > MAX_SDP_BYTES:
            self._fail("WEBRTC_NEGOTIATION_FAILED")
            return
        self.offer_sent = True
        candidates = sum(1 for line in text.splitlines() if line.startswith("a=candidate"))
        self.channel.send(
            {
                "type": "OFFER",
                "generation": self.generation,
                "at_ns": time.monotonic_ns(),
                "sdp": text,
                "candidates": candidates,
            }
        )

    def _on_offer_created(self, promise: Any, _data: Any) -> None:
        if promise.wait() != self.gst.PromiseResult.REPLIED:
            self._fail("WEBRTC_NEGOTIATION_FAILED")
            return
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None or offer.sdp is None:
            self._fail("WEBRTC_NEGOTIATION_FAILED")
            return
        self.peer.emit("set-local-description", offer, self.gst.Promise.new())

    def create_offer(self) -> None:
        self.peer.emit(
            "create-offer",
            None,
            self.gst.Promise.new_with_change_func(self._on_offer_created, None),
        )

    def apply_answer(self, sdp_text: str) -> None:
        ok, message = self.sdp_module.SDPMessage.new_from_text(sdp_text)
        if ok != self.sdp_module.SDPResult.OK or message is None:
            self._fail("WHEP_INVALID_ANSWER")
            return
        medias = [message.get_media(index).get_media() for index in range(message.medias_len())]
        if "video" not in medias:
            self._fail("WHEP_INVALID_ANSWER")
            return
        for index in range(message.medias_len()):
            media = message.get_media(index)
            if media.get_media() != "video" and media.get_port() != 0:
                self._fail("WHEP_INVALID_ANSWER")  # video-only contract
                return
        answer = self.webrtc.WebRTCSessionDescription.new(self.webrtc.WebRTCSDPType.ANSWER, message)
        self.peer.emit("set-remote-description", answer, self.gst.Promise.new())
        self.answer_applied = True

    def _on_pad_added(self, _element: Any, pad: Any) -> None:
        gst = self.gst
        if not pad.get_name().startswith("src"):
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if structure is None or structure.get_string("media") != "video":
            return
        encoding = (structure.get_string("encoding-name") or "").upper()
        route = CODEC_ROUTES.get(encoding)
        if route is None:
            self._fail("WEBRTC_CODEC_UNSUPPORTED")
            return
        names = [route[0], route[1]]
        decoder_name: str | None = None
        if self.decoder_mode == "nvidia":
            decoder_name = HARDWARE_DECODER if gst.ElementFactory.find(HARDWARE_DECODER) else None
        elif self.decoder_mode == "software":
            decoder_name = next(
                (n for n in SOFTWARE_DECODERS[encoding] if gst.ElementFactory.find(n)), None
            )
        if self.decoder_mode != "none" and decoder_name is None:
            self._fail("DECODER_START_FAILED")
            return
        if decoder_name:
            names.append(decoder_name)
        names.append("fakesink")
        elements = []
        for index, name in enumerate(names):
            element = gst.ElementFactory.make(name, f"{name}-{index}")
            if element is None:
                self._fail("DECODER_START_FAILED")
                return
            elements.append(element)
        sink = elements[-1]
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("enable-last-sample", False)  # never retain a decoded frame
        for element in elements:
            self.pipeline.add(element)
        for index in range(len(elements) - 1):
            if not elements[index].link(elements[index + 1]):
                self._fail("WEBRTC_NEGOTIATION_FAILED")
                return
        for element in elements:
            element.sync_state_with_parent()
        if pad.link(elements[0].get_static_pad("sink")) != gst.PadLinkReturn.OK:
            self._fail("WEBRTC_NEGOTIATION_FAILED")
            return
        elements[0].get_static_pad("sink").add_probe(gst.PadProbeType.BUFFER, self._rtp_probe)
        if decoder_name:
            sink.get_static_pad("sink").add_probe(gst.PadProbeType.BUFFER, self._decoded_probe)
        if not self.negotiated_sent:
            self.negotiated_sent = True
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
    def _timestamp(value: int) -> int:
        return -1 if value == CLOCK_TIME_NONE else int(value)

    def _append(self, target: list[list[int]], sample: list[int]) -> None:
        with self.lock:
            if len(self.compressed) + len(self.decoded) >= MAX_PENDING_SAMPLES:
                self.dropped_samples += 1
                return
            target.append(sample)

    def _rtp_probe(self, _pad: Any, info: Any) -> Any:
        buffer = info.get_buffer()
        if buffer is not None:
            self._append(
                self.compressed,
                [
                    time.monotonic_ns(),
                    self._timestamp(buffer.pts),
                    self._timestamp(buffer.dts),
                    buffer.get_size(),
                ],
            )
        return self.gst.PadProbeReturn.OK

    def _decoded_probe(self, pad: Any, info: Any) -> Any:
        buffer = info.get_buffer()
        if buffer is not None:
            self._append(self.decoded, [time.monotonic_ns(), self._timestamp(buffer.pts)])
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

    # --- loop ---------------------------------------------------------------------
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
            return "WEBRTC_RUNTIME_UNAVAILABLE"
        self.create_offer()
        bus = self.pipeline.get_bus()
        mask = gst.MessageType.ERROR | gst.MessageType.EOS
        started = time.monotonic()
        last_flush = last_heartbeat = started
        outcome = "STOPPED"
        while True:
            context = self.glib.MainContext.default()
            while context.pending():
                context.iteration(False)
            message = bus.timed_pop_filtered(20 * gst.MSECOND, mask)
            if self.failure is not None:
                outcome = self.failure
                break
            if message is not None:
                if message.type == gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    source = message.src.get_name() if message.src else ""
                    outcome = (
                        "DECODER_FAILED"
                        if "nvv4l2decoder" in source
                        else "WEBRTC_CONNECTION_FAILED"
                    )
                    del error
                    break
                outcome = "EOS"
                break
            now = time.monotonic()
            if not self.offer_sent and now - started > self.gather_timeout:
                outcome = "WEBRTC_ICE_FAILED"
                break
            if (
                self.offer_sent
                and not self.answer_applied
                and now - started > ANSWER_TIMEOUT_SECONDS
            ):
                outcome = "WHEP_INVALID_ANSWER"
                break
            try:
                command = self.channel.receive(0)
            except (EOFError, OSError, ValueError):
                outcome = "PARENT_GONE"
                break
            if command is not None:
                kind = command.get("type")
                if kind == "STOP":
                    outcome = "STOPPED"
                    break
                if kind == "ANSWER":
                    try:
                        self.apply_answer(validate_sdp(command.get("sdp")))
                    except ValueError:
                        outcome = "WHEP_INVALID_ANSWER"
                        break
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
                        "ice_state": self.ice_state,
                        "connection_state": self.connection_state,
                        "dropped_samples": self.dropped_samples,
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
        self.pipeline.get_state(3 * self.gst.SECOND)


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
            gst, _webrtc, _sdp, _glib = load_gstreamer()
            plugins = {
                name: gst.ElementFactory.find(name) is not None
                for name in ("webrtcbin", "nicesrc", "nicesink", HARDWARE_DECODER)
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
            if not all(plugins.values()):
                channel.send(
                    {"type": "FAILED", "generation": 0, "category": "WEBRTC_RUNTIME_UNAVAILABLE"}
                )
                return 3
        except (ImportError, ValueError):
            channel.send(
                {"type": "HELLO", "protocol_version": PROTOCOL_VERSION, "worker_pid": os.getpid()}
            )
            channel.send(
                {"type": "FAILED", "generation": 0, "category": "WEBRTC_RUNTIME_UNAVAILABLE"}
            )
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
            receiver = WebRtcReceiver(channel, start)
        except RuntimeError as exc:
            channel.send({"type": "FAILED", "generation": generation, "category": str(exc)})
            return 5
        try:
            outcome = receiver.run()
        finally:
            receiver.close()
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
