"""Synthetic WHEP control endpoint for qualification. TEST/QUALIFICATION ONLY.

Binds 127.0.0.1 on an ephemeral port, serves scripted responses so the WHEP client's security
rules can be proven without any Ring credential, and never becomes a production server. All
credentials used with it are obviously synthetic. It stores nothing and logs nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

SYNTHETIC_BEARER = "synthetic-whep-bearer-not-a-real-token"
SYNTHETIC_ANSWER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=sendonly\r\n"
    "a=mid:0\r\n"
)


@dataclass
class WhepScript:
    """One scripted response. Defaults describe a healthy session creation."""

    status: int = 201
    body: bytes = SYNTHETIC_ANSWER.encode()
    content_type: str | None = "application/sdp"
    location: str | None = None
    delay_seconds: float = 0.0
    require_bearer: bool = True


@dataclass
class Observation:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""
    query: str = ""


@dataclass
class WhepFixtureState:
    scripts: list[WhepScript] = field(default_factory=list)
    default: WhepScript = field(default_factory=WhepScript)
    observations: list[Observation] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Optional dynamic answer (V1-DEMO-03C): given the observed request, return its script or
    # None to fall back to the queue. Lets a synthetic broker answer with a real peer's SDP.
    responder: Callable[[Observation], WhepScript | None] | None = None

    def next_script(self, observation: Observation | None = None) -> WhepScript:
        responder = self.responder
        if responder is not None and observation is not None:
            dynamic = responder(observation)
            if dynamic is not None:
                return dynamic
        with self.lock:
            return self.scripts.pop(0) if self.scripts else self.default


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: WhepFixtureState

    def log_message(self, *_: Any) -> None:  # never log request lines
        return None

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 1_048_576)) if length else b""
        path, _, query = self.path.partition("?")
        observation = Observation(
            method, path, {k.lower(): v for k, v in self.headers.items()}, body, query
        )
        with self.state.lock:
            self.state.observations.append(observation)
        script = self.state.next_script(observation)
        if script.delay_seconds:
            threading.Event().wait(script.delay_seconds)
        if (
            script.require_bearer
            and self.headers.get("Authorization") != f"Bearer {SYNTHETIC_BEARER}"
        ):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(script.status)
        if script.content_type:
            self.send_header("Content-Type", script.content_type)
        if script.location:
            self.send_header("Location", script.location)
        self.send_header("Content-Length", str(len(script.body)))
        self.end_headers()
        if script.body:
            self.wfile.write(script.body)

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_GET(self) -> None:
        self._handle("GET")


class _QuietServer(HTTPServer):
    """Clients disconnect early in failure tests; that is expected, not a fixture error."""

    def handle_error(self, *_: object) -> None:
        return None


class WhepFixtureServer:
    """Loopback-only scripted WHEP endpoint."""

    def __init__(self) -> None:
        self.state = WhepFixtureState()
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self._server = _QuietServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def script(self, *scripts: WhepScript) -> None:
        with self.state.lock:
            self.state.scripts.extend(scripts)

    def observations(self) -> list[Observation]:
        with self.state.lock:
            return list(self.state.observations)

    def connection_factory(self) -> Any:
        """Plain-HTTP connection factory for protocol tests (TLS config is asserted separately)."""
        import http.client

        def factory(_host: str, _port: int, timeout: float) -> Any:
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

        return factory

    def stats(self) -> dict[str, Any]:
        return {"requests": len(self.observations())}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> WhepFixtureServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
