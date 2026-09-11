#!/usr/bin/python3
"""Synthetic loopback RTSP fixture for R5A transport qualification. QUALIFICATION ONLY.

Serves only in-memory ``videotestsrc`` content encoded live by the installed x264/x265
encoders as RTP interleaved over RTSP/TCP (the transport mode Ring documents for RTSPS). It
binds 127.0.0.1 only, requires Basic authentication with a synthetic password read from stdin
(never argv/env), and writes nothing to disk. Fault injection and statistics use stdin/stdout
JSON lines. A second loopback "canary" listener records whether a client can be induced to
connect elsewhere (redirect or cross-origin control URL) and whether it would present
credentials there; it records only counts, never header values.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hmac
import json
import socket
import sys
import threading
import time
from typing import Any

USERNAME = "veotrex-fixture"
MAX_REQUEST_BYTES = 16_384


def load_gstreamer() -> Any:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore[import-not-found]

    Gst.init(None)
    return Gst


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, int] = {
            "connections_total": 0,
            "sessions_total": 0,
            "active_sessions": 0,
            "auth_failures": 0,
            "auth_successes": 0,
            "packets_sent": 0,
            "bytes_sent": 0,
            "sessions_expired": 0,
            "sessions_dropped": 0,
            "redirects_sent": 0,
            "canary_connections": 0,
            "canary_auth_headers": 0,
            "udp_setup_rejected": 0,
        }

    def add(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.values[key] += amount

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.values)


class Fixture:
    def __init__(self, arguments: argparse.Namespace, password: str) -> None:
        self.args = arguments
        self._password = password
        self.stats = Stats()
        self.gst = load_gstreamer()
        self.stall_until = 0.0
        self.max_session_seconds = float(arguments.max_session_seconds)
        self.redirect = False
        self.cross_origin_control = False
        self.clients: set[Client] = set()
        self.clients_lock = threading.Lock()
        self.server = self._listen()
        self.canary = self._listen()
        self.port = self.server.getsockname()[1]
        self.canary_port = self.canary.getsockname()[1]

    @staticmethod
    def _listen() -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        return server

    def authorized(self, headers: dict[str, str]) -> bool:
        value = headers.get("authorization", "")
        if not value.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(value[6:].strip(), validate=True).decode()
        except (ValueError, UnicodeError):
            return False
        user, _, secret = decoded.partition(":")
        return hmac.compare_digest(user, USERNAME) and hmac.compare_digest(secret, self._password)

    def pipeline_description(self) -> str:
        a = self.args
        caps = f"video/x-raw,format=I420,width={a.width},height={a.height},framerate={a.fps}/1"
        key = max(1, a.fps * 2)
        if a.codec == "h265":
            encoder = (
                f"x265enc tune=zerolatency speed-preset=ultrafast key-int-max={key} "
                f"bitrate={a.bitrate_kbps} ! h265parse config-interval=-1 ! "
                "rtph265pay pt=96 config-interval=-1 mtu=1400"
            )
        else:
            encoder = (
                f"x264enc tune=zerolatency speed-preset=ultrafast key-int-max={key} "
                f"bitrate={a.bitrate_kbps} ! h264parse config-interval=-1 ! "
                "rtph264pay pt=96 config-interval=-1 mtu=1400"
            )
        return (
            f"videotestsrc is-live=true pattern=ball ! {caps} ! {encoder} ! "
            "appsink name=sink emit-signals=false sync=false max-buffers=1000 drop=true"
        )

    def accept_loop(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.stats.add("connections_total")
            client = Client(self, connection)
            with self.clients_lock:
                self.clients.add(client)
            threading.Thread(target=client.run, daemon=True).start()

    def canary_loop(self) -> None:
        while True:
            try:
                connection, _ = self.canary.accept()
            except OSError:
                return
            self.stats.add("canary_connections")
            with contextlib.suppress(OSError):
                connection.settimeout(2.0)
                data = connection.recv(MAX_REQUEST_BYTES)
                if b"\nauthorization:" in data.lower():
                    self.stats.add("canary_auth_headers")
                connection.sendall(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
            connection.close()

    def drop_all(self) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            self.stats.add("sessions_dropped")
            client.close()


class Client:
    def __init__(self, fixture: Fixture, connection: socket.socket) -> None:
        self.fixture = fixture
        self.connection = connection
        self.write_lock = threading.Lock()
        self.session_id = f"{id(self) & 0xFFFFFFFF:08x}"
        self.pipeline: Any = None
        self.active = False
        self.closed = False
        self.play_started = 0.0

    def send(self, data: bytes) -> bool:
        with self.write_lock:
            if self.closed:
                return False
            try:
                self.connection.sendall(data)
            except OSError:
                return False
        return True

    def respond(
        self,
        cseq: str,
        code: int,
        reason: str,
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}", "Server: VeoTrex-synthetic-fixture"]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body.encode())}")
        self.send(("\r\n".join(lines) + "\r\n\r\n" + body).encode())

    def sdp(self) -> str:
        fixture = self.fixture
        encoding = "H265" if fixture.args.codec == "h265" else "H264"
        control = (
            f"rtsp://127.0.0.1:{fixture.canary_port}/stream/trackID=0"
            if fixture.cross_origin_control
            else "trackID=0"
        )
        fmtp = "a=fmtp:96 packetization-mode=1\r\n" if encoding == "H264" else ""
        return (
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=VeoTrex synthetic fixture\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n"
            f"m=video 0 RTP/AVP 96\r\na=rtpmap:96 {encoding}/90000\r\n{fmtp}"
            f"a=control:{control}\r\n"
        )

    def handle(self, method: str, headers: dict[str, str]) -> bool:
        fixture = self.fixture
        cseq = headers.get("cseq", "0")
        if method == "OPTIONS":
            self.respond(
                cseq,
                200,
                "OK",
                {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"},
            )
            return True
        if method == "DESCRIBE":
            if fixture.redirect:
                fixture.stats.add("redirects_sent")
                self.respond(
                    cseq,
                    302,
                    "Moved Temporarily",
                    {"Location": f"rtsp://127.0.0.1:{fixture.canary_port}/stream"},
                )
                return True
            if not fixture.authorized(headers):
                fixture.stats.add("auth_failures")
                self.respond(
                    cseq, 401, "Unauthorized", {"WWW-Authenticate": 'Basic realm="veotrex-fixture"'}
                )
                return True
            fixture.stats.add("auth_successes")
            self.respond(
                cseq,
                200,
                "OK",
                {
                    "Content-Type": "application/sdp",
                    "Content-Base": f"rtsp://127.0.0.1:{fixture.port}/stream/",
                },
                self.sdp(),
            )
            return True
        if method == "SETUP":
            transport = headers.get("transport", "")
            if "RTP/AVP/TCP" not in transport.upper() or "interleaved" not in transport.lower():
                fixture.stats.add("udp_setup_rejected")
                self.respond(cseq, 461, "Unsupported Transport")
                return True
            self.respond(
                cseq,
                200,
                "OK",
                {
                    "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
                    "Session": f"{self.session_id};timeout=60",
                },
            )
            return True
        if method == "PLAY":
            self.respond(cseq, 200, "OK", {"Session": self.session_id, "Range": "npt=0.000-"})
            self.start_media()
            return True
        if method in {"GET_PARAMETER", "SET_PARAMETER"}:
            self.respond(cseq, 200, "OK", {"Session": self.session_id})
            return True
        if method == "TEARDOWN":
            self.respond(cseq, 200, "OK", {"Session": self.session_id})
            return False
        self.respond(cseq, 405, "Method Not Allowed")
        return True

    def start_media(self) -> None:
        if self.active:
            return
        gst = self.fixture.gst
        self.pipeline = gst.parse_launch(self.fixture.pipeline_description())
        self.pipeline.set_state(gst.State.PLAYING)
        self.active = True
        self.play_started = time.monotonic()
        self.fixture.stats.add("sessions_total")
        self.fixture.stats.add("active_sessions")
        threading.Thread(target=self.media_loop, daemon=True).start()

    def media_loop(self) -> None:
        fixture = self.fixture
        gst = fixture.gst
        sink = self.pipeline.get_by_name("sink")
        while self.active and not self.closed:
            sample = sink.emit("try-pull-sample", 100 * gst.MSECOND)
            limit = fixture.max_session_seconds
            if limit > 0 and time.monotonic() - self.play_started >= limit:
                fixture.stats.add("sessions_expired")
                self.close()
                return
            if sample is None or time.monotonic() < fixture.stall_until:
                continue
            buffer = sample.get_buffer()
            ok, info = buffer.map(gst.MapFlags.READ)
            if not ok:
                continue
            try:
                payload = bytes(info.data)
            finally:
                buffer.unmap(info)
            if len(payload) > 65_535:
                continue
            frame = b"$\x00" + len(payload).to_bytes(2, "big") + payload
            if not self.send(frame):
                self.close()
                return
            fixture.stats.add("packets_sent")
            fixture.stats.add("bytes_sent", len(frame))

    def run(self) -> None:
        buffer = b""
        self.connection.settimeout(1.0)
        try:
            while not self.closed:
                try:
                    chunk = self.connection.recv(65_536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 4 * MAX_REQUEST_BYTES:
                    break
                keep_going = True
                while buffer and keep_going:
                    if buffer[:1] == b"$":
                        if len(buffer) < 4:
                            break
                        size = int.from_bytes(buffer[2:4], "big")
                        if len(buffer) < 4 + size:
                            break
                        buffer = buffer[4 + size :]
                        continue
                    end = buffer.find(b"\r\n\r\n")
                    if end < 0:
                        break
                    head = buffer[:end].decode("latin-1")
                    lines = head.split("\r\n")
                    headers: dict[str, str] = {}
                    for line in lines[1:]:
                        key, _, value = line.partition(":")
                        headers[key.strip().lower()] = value.strip()
                    length = int(headers.get("content-length", "0") or 0)
                    if len(buffer) < end + 4 + length:
                        break
                    buffer = buffer[end + 4 + length :]
                    method = lines[0].split(" ", 1)[0].upper()
                    keep_going = self.handle(method, headers)
                if not keep_going:
                    break
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        was_active, self.active = self.active, False
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.connection.close()
        if self.pipeline is not None:
            self.pipeline.set_state(self.fixture.gst.State.NULL)
        if was_active:
            self.fixture.stats.add("active_sessions", -1)
        with self.fixture.clients_lock:
            self.fixture.clients.discard(self)


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codec", choices=("h264", "h265"), default="h264")
    parser.add_argument("--width", type=int, default=1920, choices=(640, 1280, 1920))
    parser.add_argument("--height", type=int, default=1080, choices=(360, 720, 1080))
    parser.add_argument("--fps", type=int, default=15, choices=(5, 10, 15, 20, 25, 30))
    parser.add_argument("--bitrate-kbps", type=int, default=2000)
    parser.add_argument("--max-session-seconds", type=float, default=0.0)
    arguments = parser.parse_args()
    if (
        not 100 <= arguments.bitrate_kbps <= 20_000
        or not 0 <= arguments.max_session_seconds <= 3600
    ):
        return 2
    password = sys.stdin.readline().rstrip("\n")
    if len(password) < 16:
        return 2
    fixture = Fixture(arguments, password)
    password = ""
    threading.Thread(target=fixture.accept_loop, daemon=True).start()
    threading.Thread(target=fixture.canary_loop, daemon=True).start()
    emit({"event": "listening", "port": fixture.port, "canary_port": fixture.canary_port})
    for line in sys.stdin:
        command, _, value = line.strip().partition(" ")
        if command == "stall":
            fixture.stall_until = time.monotonic() + min(600.0, max(0.0, float(value)))
            emit({"event": "ack", "command": "stall"})
        elif command == "drop":
            fixture.drop_all()
            emit({"event": "ack", "command": "drop"})
        elif command == "expire":
            fixture.max_session_seconds = min(3600.0, max(0.0, float(value)))
            emit({"event": "ack", "command": "expire"})
        elif command == "redirect":
            fixture.redirect = value == "on"
            emit({"event": "ack", "command": "redirect"})
        elif command == "cross-origin-control":
            fixture.cross_origin_control = value == "on"
            emit({"event": "ack", "command": "cross-origin-control"})
        elif command == "stats":
            emit({"event": "stats", **fixture.stats.snapshot()})
        elif command == "quit":
            break
    fixture.drop_all()
    fixture.server.close()
    fixture.canary.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
