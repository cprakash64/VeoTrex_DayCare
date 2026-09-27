"""A narrow JSON client for the two V1-05B machine routes of the control plane.

It lives outside ``live/`` on purpose: no ``live`` module performs HTTP or imports the broker
transport itself (V1-DEMO-03C boundary); managed mode reaches the control plane only through this
client and ``resolve_control_plane``.

Only two requests exist, and the client refuses to make any other:

    GET  /v1/edge/runtime-config             this node's portal configuration
    POST /v1/edge/events/room-transitions    anonymous room entry/exit events

It reuses the broker's building blocks unchanged (``camera_transport.broker_whep``): the origin
comes from ``ControlPlaneEndpoint`` (HTTPS only, bare origin, public host outside local/test),
the TLS connection from the WHEP client's factory (certificate verification required), and the
machine credential from ``read_edge_credential`` - read from its 0600 file at the moment of each
request and placed only in the ``Authorization`` header. Standard-library ``http.client`` never
follows redirects, and a 3xx is refused outright, so a credential can never be carried to
another origin. Responses are bounded in size and must be JSON. Every failure is a
``ControlPlaneError`` with a fixed category; no URL, header, body or credential is ever part of
an error or a log line.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import ssl
import time
from collections.abc import Callable
from typing import Any

from veotrex_edge_agent.camera_transport.broker_whep import (
    ControlPlaneEndpoint,
    EdgeCredentialError,
    read_edge_credential,
)
from veotrex_edge_agent.camera_transport.whep_client import ConnectionFactory, _default_connection

__all__ = [
    "ROOM_TRANSITIONS_PATH",
    "RUNTIME_CONFIG_PATH",
    "ConnectionFactory",
    "ControlPlaneEndpoint",
    "ControlPlaneError",
    "EdgeControlPlaneClient",
    "resolve_control_plane",
]

RUNTIME_CONFIG_PATH = "/v1/edge/runtime-config"
ROOM_TRANSITIONS_PATH = "/v1/edge/events/room-transitions"
ALLOWED = {("GET", RUNTIME_CONFIG_PATH), ("POST", ROOM_TRANSITIONS_PATH)}
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 262_144
READ_CHUNK_BYTES = 16_384
MAX_REQUEST_BYTES = 65_536
USER_AGENT = "VeoTrex-Edge/1.0 (runtime)"


# Worth trying again later. "endpoint_unavailable" (404/405/408) is here on purpose: a control
# plane that does not have these routes yet, or a proxy in the way, must never turn queued events
# into permanent rejections.
RETRYABLE_CATEGORIES = frozenset(
    {"transport_failed", "server_error", "rate_limited", "endpoint_unavailable"}
)


class ControlPlaneError(Exception):
    """A bounded, secret-free failure. ``category`` is fixed text; ``status`` is the HTTP code
    when there was one."""

    def __init__(self, category: str, status: int | None = None) -> None:
        super().__init__(category if status is None else f"{category} ({status})")
        self.category = category
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


def _category(status: int) -> str:
    if 300 <= status < 400:
        return "redirect_refused"
    if status in (401, 403):
        return "auth_rejected"
    if status == 429:
        return "rate_limited"
    if status in (404, 405, 408):
        return "endpoint_unavailable"
    if status >= 500:
        return "server_error"
    return "request_rejected"


def resolve_control_plane(
    url: str | None, credential_file: str | None, *, environment: str
) -> ControlPlaneEndpoint:
    """Validate the origin and the credential file (read once, value discarded) before anything
    starts. Raises ``ControlPlaneError`` with the same fixed categories the Ring source uses."""
    if not url:
        raise ControlPlaneError("control_plane_url_not_configured")
    try:
        endpoint = ControlPlaneEndpoint.parse(url, environment=environment)
    except ValueError:
        raise ControlPlaneError("control_plane_url_invalid") from None
    if not credential_file:
        raise ControlPlaneError("credential_file_not_configured")
    try:
        read_edge_credential(credential_file)
    except EdgeCredentialError as exc:
        raise ControlPlaneError(exc.reason) from None
    return endpoint


class EdgeControlPlaneClient:
    def __init__(
        self,
        endpoint: ControlPlaneEndpoint,
        credential_file: str,
        *,
        connection_factory: ConnectionFactory = _default_connection,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        self.endpoint = endpoint
        self._credential_file = credential_file
        self._connections = connection_factory
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes
        self._monotonic = monotonic

    def __repr__(self) -> str:  # never the credential path's content, never a token
        return f"EdgeControlPlaneClient(origin={self.endpoint.origin})"

    def get_json(self, path: str) -> Any:
        return self._request("GET", path, None)

    def post_json(self, path: str, document: Any) -> Any:
        body = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ControlPlaneError("request_too_large")
        return self._request("POST", path, body)

    def _read_bounded(self, response: Any) -> bytes:
        """The body, refused past ``max_response_bytes`` or past the request's time budget.

        The socket timeout bounds each read; this bounds their sum, so a server that trickles
        one byte just inside the timeout cannot hold the caller indefinitely.
        """
        deadline = self._monotonic() + self._timeout
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = response.read(min(READ_CHUNK_BYTES, self._max_response + 1 - received))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            received += len(chunk)
            if received > self._max_response:
                raise ControlPlaneError("response_too_large")
            if self._monotonic() > deadline:
                raise ControlPlaneError("transport_failed")

    def _request(self, method: str, path: str, body: bytes | None) -> Any:
        if (method, path) not in ALLOWED:
            raise ControlPlaneError("path_not_allowed")
        try:
            token = read_edge_credential(self._credential_file)
        except EdgeCredentialError:
            raise ControlPlaneError("credential_unavailable") from None
        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Host": self.endpoint.host,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = None
        try:
            connection = self._connections(self.endpoint.host, self.endpoint.port, self._timeout)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = int(response.status)
            if not 200 <= status < 300:
                raise ControlPlaneError(_category(status), status)
            media = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                raise ControlPlaneError("unexpected_media_type", status)
            content = self._read_bounded(response)
        except ControlPlaneError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
            # Resolution, connection, TLS, timeout or protocol failure: all the same to callers.
            raise ControlPlaneError("transport_failed") from None
        finally:
            del headers, token
            if connection is not None:
                with contextlib.suppress(Exception):  # closing must never mask the outcome
                    connection.close()
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlPlaneError("malformed_response") from None
