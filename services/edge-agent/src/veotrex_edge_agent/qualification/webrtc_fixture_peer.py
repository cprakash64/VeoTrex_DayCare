#!/usr/bin/python3
"""Synthetic local WebRTC sending peer. TEST/QUALIFICATION ONLY.

Answers a complete (non-trickle) SDP offer with a complete SDP answer and sends synthetic
``videotestsrc`` H.264 media over WebRTC on loopback/host candidates only. Signaling is
deterministic JSON lines on stdin/stdout, entirely in-process for the qualification harness: no
network signaling server, no STUN/TURN, no Internet dependency, and nothing written to disk.

Fault injection lets the harness exercise the negative matrix (malformed/audio/unsupported-codec
answers, media stalls, abrupt peer loss) without touching any real provider.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any

MAX_SDP_BYTES = 61_440
MODES = frozenset(
    {"normal", "malformed-answer", "audio-answer", "unsupported-codec", "no-media", "empty-answer"}
)


def load_gstreamer() -> tuple[Any, Any, Any, Any]:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # type: ignore[import-not-found]

    Gst.init(None)
    return Gst, GstWebRTC, GstSdp, GLib


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()


class SendingPeer:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.args = arguments
        self.gst, self.webrtc, self.sdp_module, self.glib = load_gstreamer()
        self.stall_until = 0.0
        self.sent_buffers = 0
        self.answer_sent = False
        self.lock = threading.Lock()
        self.pipeline = self.gst.Pipeline.new("veotrex-fixture-sender")
        peer = self.gst.ElementFactory.make("webrtcbin", "sender")
        if peer is None:
            raise RuntimeError("webrtcbin unavailable")
        self.peer = peer
        peer.set_property("bundle-policy", "max-bundle")
        self.pipeline.add(peer)
        elements = self._build_source()
        for element in elements:
            self.pipeline.add(element)
        for index in range(len(elements) - 1):
            if not elements[index].link(elements[index + 1]):
                raise RuntimeError("fixture source link failed")
        if not elements[-1].link(peer):
            raise RuntimeError("fixture payloader link failed")
        elements[-1].get_static_pad("src").add_probe(self.gst.PadProbeType.BUFFER, self._send_probe)
        peer.connect("notify::ice-gathering-state", self._on_gathering)

    def _build_source(self) -> list[Any]:
        gst = self.gst
        a = self.args
        encoder = "x265enc" if a.codec == "H265" else "x264enc"
        payloader = "rtph265pay" if a.codec == "H265" else "rtph264pay"
        source = gst.ElementFactory.make("videotestsrc", "src")
        source.set_property("is-live", True)
        source.set_property("pattern", 18)  # ball: moving content, deterministic and synthetic
        caps_filter = gst.ElementFactory.make("capsfilter", "caps")
        caps_filter.set_property(
            "caps",
            gst.Caps.from_string(
                f"video/x-raw,format=I420,width={a.width},height={a.height},framerate={a.fps}/1"
            ),
        )
        convert = gst.ElementFactory.make("videoconvert", "convert")
        encode = gst.ElementFactory.make(encoder, "encode")
        if encode is None:
            raise RuntimeError(f"{encoder} unavailable")
        encode.set_property("bitrate", a.bitrate_kbps)
        for name, value in (("tune", "zerolatency"), ("speed-preset", "ultrafast")):
            if encode.find_property(name) is not None:
                encode.set_property(name, value)
        if encode.find_property("key-int-max") is not None:
            encode.set_property("key-int-max", max(1, a.fps * 2))
        pay = gst.ElementFactory.make(payloader, "pay")
        pay.set_property("config-interval", -1)
        pay.set_property("pt", 96)
        rtp_caps = gst.ElementFactory.make("capsfilter", "rtpcaps")
        rtp_caps.set_property(
            "caps",
            gst.Caps.from_string(
                f"application/x-rtp,media=video,encoding-name={a.codec},payload=96,clock-rate=90000"
            ),
        )
        return [source, caps_filter, convert, encode, pay, rtp_caps]

    def _send_probe(self, _pad: Any, info: Any) -> Any:
        if time.monotonic() < self.stall_until:
            return self.gst.PadProbeReturn.DROP  # injected media stall
        if info.get_buffer() is not None:
            self.sent_buffers += 1
        return self.gst.PadProbeReturn.OK

    # --- negotiation ---------------------------------------------------------------
    def accept_offer(self, sdp_text: str) -> None:
        ok, message = self.sdp_module.SDPMessage.new_from_text(sdp_text)
        if ok != self.sdp_module.SDPResult.OK or message is None:
            emit({"event": "error", "detail": "offer_parse_failed"})
            return
        offer = self.webrtc.WebRTCSessionDescription.new(self.webrtc.WebRTCSDPType.OFFER, message)
        self.peer.emit("set-remote-description", offer, self.gst.Promise.new())
        self.peer.emit(
            "create-answer", None, self.gst.Promise.new_with_change_func(self._on_answer, None)
        )

    def _on_answer(self, promise: Any, _data: Any) -> None:
        if promise.wait() != self.gst.PromiseResult.REPLIED:
            emit({"event": "error", "detail": "answer_promise_failed"})
            return
        reply = promise.get_reply()
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None or answer.sdp is None:
            emit({"event": "error", "detail": "answer_missing"})
            return
        self.peer.emit("set-local-description", answer, self.gst.Promise.new())

    def _on_gathering(self, *_: Any) -> None:
        state = self.peer.get_property("ice-gathering-state")
        if state != self.webrtc.WebRTCICEGatheringState.COMPLETE or self.answer_sent:
            return
        description = self.peer.get_property("local-description")
        if description is None or description.sdp is None:
            emit({"event": "error", "detail": "local_description_missing"})
            return
        self.answer_sent = True
        emit({"event": "answer", "sdp": self._mutate(description.sdp.as_text())})

    def _mutate(self, sdp: str) -> str:
        """Fault injection for the negative matrix; 'normal' returns the real answer."""
        mode = self.args.mode
        if mode == "malformed-answer":
            return "this is not an sdp document"
        if mode == "empty-answer":
            return ""
        if mode == "audio-answer":
            return sdp + "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\na=sendonly\r\n"
        if mode == "unsupported-codec":
            return sdp.replace("H264", "VP8").replace("H265", "VP8")
        return sdp

    def start(self) -> None:
        if self.args.mode == "no-media":
            self.stall_until = time.monotonic() + 3_600
        self.pipeline.set_state(self.gst.State.PLAYING)

    def stop(self) -> None:
        self.pipeline.set_state(self.gst.State.NULL)
        self.pipeline.get_state(3 * self.gst.SECOND)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codec", choices=("H264", "H265"), default="H264")
    parser.add_argument("--width", type=int, choices=(320, 640, 1280, 1920), default=1280)
    parser.add_argument("--height", type=int, choices=(240, 360, 720, 1080), default=720)
    parser.add_argument("--fps", type=int, choices=(5, 10, 15, 20, 25, 30), default=15)
    parser.add_argument("--bitrate-kbps", type=int, default=1500)
    parser.add_argument("--mode", choices=sorted(MODES), default="normal")
    arguments = parser.parse_args()
    if not 100 <= arguments.bitrate_kbps <= 20_000:
        return 2
    peer = SendingPeer(arguments)
    peer.start()
    emit({"event": "ready", "mode": arguments.mode, "codec": arguments.codec})
    glib = peer.glib
    context = glib.MainContext.default()
    try:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except ValueError:
                continue
            kind = command.get("type")
            if kind == "OFFER":
                sdp = command.get("sdp")
                if isinstance(sdp, str) and 0 < len(sdp) <= MAX_SDP_BYTES:
                    peer.accept_offer(sdp)
                deadline = time.monotonic() + 20
                while not peer.answer_sent and time.monotonic() < deadline:
                    context.iteration(False)
                    time.sleep(0.005)
            elif kind == "STALL":
                peer.stall_until = time.monotonic() + float(command.get("seconds", 0) or 0)
                emit({"event": "ack", "command": "stall"})
            elif kind == "STATS":
                emit({"event": "stats", "sent_buffers": peer.sent_buffers})
            elif kind == "STOP":
                break
    finally:
        peer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
