"""WHEP control-plane client for official Ring live video.

Standard library only: adding an HTTP dependency to the edge agent would be a separate runtime
gate. `http.client` also never follows redirects, which is the behaviour we want.

Verified against current official Ring material on 2026-09-11:
``POST https://api.amazonvision.com/v1/devices/{device_id}/media/streaming/whep/sessions`` with
``Authorization: Bearer <token>`` and ``Content-Type: application/sdp``; the response body is the
SDP answer and the session resource arrives in the ``Location`` header; teardown is
``DELETE .../whep/sessions/{session_id}``. Ring does not currently document the success status
code, Location format, ICE/STUN/TURN behaviour, session lifetime, or codecs, so this client
accepts any 2xx carrying a valid SDP answer and never invents the undocumented parts.
"""

from __future__ import annotations

import http.client
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory

RING_WHEP_HOST = "api.amazonvision.com"
RING_WHEP_PORT = 443
_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_COMPONENT_ID = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
_SESSION_PATH = re.compile(
    r"^/v1/devices/[A-Za-z0-9._~%-]{1,256}/media/streaming/whep/sessions/[A-Za-z0-9._~%-]{1,256}$"
)
_SDP_LINE = re.compile(r"^[a-z]=.*$")
_C = TransportErrorCategory
_STATUS_CATEGORIES = {
    400: _C.WHEP_OFFER_FAILED,
    401: _C.WHEP_HTTP_UNAUTHORIZED,
    403: _C.WHEP_HTTP_FORBIDDEN,
    404: _C.CAMERA_OFFLINE,
    429: _C.WHEP_HTTP_RATE_LIMITED,
    503: _C.CAMERA_OFFLINE,
}


@dataclass(frozen=True, slots=True)
class WhepConfig:
    """Bounded control-plane limits. Ring documents none of these, so they are ours."""

    host: str = RING_WHEP_HOST
    port: int = RING_WHEP_PORT
    connect_timeout_seconds: float = 5.0
    total_timeout_seconds: float = 20.0
    max_sdp_bytes: int = 65_536
    max_header_bytes: int = 8_192
    user_agent: str = "VeoTrex-Edge/1.0"

    def __post_init__(self) -> None:
        if not _HOSTNAME.fullmatch(self.host or ""):
            raise ValueError("invalid WHEP host")
        if not 1 <= self.port <= 65_535:
            raise ValueError("invalid WHEP port")
        if not 0 < self.connect_timeout_seconds <= self.total_timeout_seconds <= 120:
            raise ValueError("invalid WHEP timeouts")
        if not 1_024 <= self.max_sdp_bytes <= 1_048_576:
            raise ValueError("SDP bound is out of range")

    @property
    def origin(self) -> tuple[str, str, int]:
        return ("https", self.host, self.port)


@dataclass(frozen=True, slots=True, repr=False)
class WhepSession:
    """Result of a WHEP session creation. The session URL is sensitive; the SDP is not logged."""

    status: int
    answer_sdp: str = field(repr=False)
    session_url: str | None = field(default=None, repr=False)
    video_codecs: tuple[str, ...] = ()

    @property
    def teardown_supported(self) -> bool:
        return self.session_url is not None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer_bytes": len(self.answer_sdp),
            "video_codecs": list(self.video_codecs),
            "session_resource": "present" if self.session_url else "absent",
        }

    def __repr__(self) -> str:
        return f"WhepSession(status={self.status}, session_resource=REDACTED, sdp=REDACTED)"

    __str__ = __repr__


ConnectionFactory = Callable[[str, int, float], Any]


def _default_connection(host: str, port: int, timeout: float) -> Any:
    # Verified TLS: hostname checking and certificate validation stay enabled.
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


class WhepClient:
    """Creates and deletes Ring WHEP sessions. The Bearer token lives only in a request header."""

    def __init__(
        self,
        config: WhepConfig | None = None,
        *,
        connection_factory: ConnectionFactory = _default_connection,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or WhepConfig()
        self._connect = connection_factory
        self._clock = clock

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fail(category: TransportErrorCategory) -> TransportError:
        return TransportError(category)

    def session_path(self, device_id: str, component_id: str | None = None) -> str:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise self._fail(_C.INVALID_ENDPOINT)
        path = f"/v1/devices/{quote(device_id, safe='._~-')}/media/streaming/whep/sessions"
        if component_id is not None:
            if not isinstance(component_id, str) or not _COMPONENT_ID.fullmatch(component_id):
                raise self._fail(_C.INVALID_ENDPOINT)
            path += f"?component_id={quote(component_id, safe='._~-')}"
        return path

    def _headers(self, bearer: SecretStr, extra: dict[str, str]) -> dict[str, str]:
        token = bearer.get_secret_value() if isinstance(bearer, SecretStr) else ""
        if not token or any(not 32 < ord(c) < 127 for c in token) or len(token) > 8_192:
            raise self._fail(_C.WHEP_HTTP_UNAUTHORIZED)
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.config.user_agent,
            "Accept": "application/sdp",
            **extra,
        }

    def validate_location(self, raw: object) -> str:
        """Accept only an absolute or relative session resource on the configured Ring origin."""
        if not isinstance(raw, str) or not 0 < len(raw) <= 1_024:
            raise self._fail(_C.WHEP_INVALID_LOCATION)
        if any(not 32 < ord(character) < 127 for character in raw) or "#" in raw:
            raise self._fail(_C.WHEP_INVALID_LOCATION)
        if raw.startswith("/"):
            path, query = (*raw.split("?", 1), "")[:2]
        else:
            parts = urlsplit(raw)
            try:
                port = parts.port or (443 if parts.scheme == "https" else None)
            except ValueError:
                raise self._fail(_C.WHEP_INVALID_LOCATION) from None
            if parts.scheme != "https":  # never downgrade, never another scheme
                raise self._fail(_C.WHEP_INVALID_LOCATION)
            if "@" in parts.netloc or parts.username or parts.password:
                raise self._fail(_C.WHEP_INVALID_LOCATION)
            if (parts.hostname or "").lower() != self.config.host or port != self.config.port:
                raise self._fail(_C.WHEP_INVALID_LOCATION)  # cross-origin session resource
            path, query = parts.path, parts.query
        if not _SESSION_PATH.fullmatch(path):
            raise self._fail(_C.WHEP_INVALID_LOCATION)
        if query and not re.fullmatch(r"[A-Za-z0-9._~%=&-]{0,256}", query):
            raise self._fail(_C.WHEP_INVALID_LOCATION)
        return f"https://{self.config.host}:{self.config.port}{path}" + (
            f"?{query}" if query else ""
        )

    def validate_answer(self, body: bytes, content_type: str | None) -> tuple[str, tuple[str, ...]]:
        if not body or len(body) > self.config.max_sdp_bytes:
            raise self._fail(_C.WHEP_INVALID_ANSWER)
        if content_type is not None:
            kind = content_type.split(";", 1)[0].strip().lower()
            if kind and kind != "application/sdp":
                raise self._fail(_C.WHEP_INVALID_ANSWER)
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeError:
            raise self._fail(_C.WHEP_INVALID_ANSWER) from None
        lines = [line for line in text.replace("\r\n", "\n").split("\n") if line]
        if (
            not lines
            or not lines[0].startswith("v=0")
            or any(not _SDP_LINE.fullmatch(line) for line in lines)
        ):
            raise self._fail(_C.WHEP_INVALID_ANSWER)
        media = [line for line in lines if line.startswith("m=")]
        video = [line for line in media if line.startswith("m=video ")]
        if not video:
            raise self._fail(_C.WHEP_INVALID_ANSWER)
        # Our offer is video-only; an accepted (non-zero port) non-video stream breaks that
        # contract. A rejected stream (port 0) is fine.
        for line in media:
            parts = line.split()
            if not line.startswith("m=video ") and len(parts) > 1 and parts[1] != "0":
                raise self._fail(_C.WHEP_INVALID_ANSWER)
        codecs = tuple(
            sorted(
                {
                    line.split(" ", 1)[1].split("/", 1)[0].strip().upper()
                    for line in lines
                    if line.startswith("a=rtpmap:") and " " in line
                }
            )
        )
        return text, codecs

    def _request(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        deadline = self._clock() + self.config.total_timeout_seconds
        connection = self._connect(
            self.config.host, self.config.port, self.config.connect_timeout_seconds
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if self._clock() > deadline:
                raise self._fail(_C.SESSION_ACQUIRE_TIMEOUT)
            payload = response.read(self.config.max_sdp_bytes + 1)
            status = int(response.status)
            collected = {
                key.lower(): value[: self.config.max_header_bytes]
                for key, value in response.getheaders()
                if key.lower() in {"location", "content-type", "retry-after"}
            }
        except TransportError:
            raise
        except (TimeoutError, OSError) as exc:
            category = (
                _C.SESSION_ACQUIRE_TIMEOUT
                if isinstance(exc, TimeoutError)
                else _C.PROVIDER_UNAVAILABLE
            )
            raise self._fail(category) from None
        except http.client.HTTPException:
            raise self._fail(_C.PROVIDER_UNAVAILABLE) from None
        finally:
            with _suppress_close():
                connection.close()
        if len(payload) > self.config.max_sdp_bytes:
            raise self._fail(_C.WHEP_INVALID_ANSWER)
        return status, collected, payload

    def _map_status(self, status: int) -> TransportErrorCategory:
        if status in _STATUS_CATEGORIES:
            return _STATUS_CATEGORIES[status]
        if 300 <= status < 400:
            return _C.REDIRECT_REFUSED  # never follow a provider redirect with a Bearer token
        if 500 <= status < 600:
            return _C.WHEP_HTTP_SERVER_ERROR
        return _C.WHEP_OFFER_FAILED

    # ------------------------------------------------------------------ operations
    def create_session(
        self,
        device_id: str,
        bearer: SecretStr,
        offer_sdp: str,
        *,
        component_id: str | None = None,
    ) -> WhepSession:
        if not isinstance(offer_sdp, str) or not offer_sdp.startswith("v=0"):
            raise self._fail(_C.WHEP_OFFER_FAILED)
        body = offer_sdp.encode("utf-8")
        if len(body) > self.config.max_sdp_bytes:
            raise self._fail(_C.WHEP_OFFER_FAILED)
        path = self.session_path(device_id, component_id)
        headers = self._headers(
            bearer, {"Content-Type": "application/sdp", "Content-Length": str(len(body))}
        )
        status, response_headers, payload = self._request("POST", path, headers, body)
        if not 200 <= status < 300:
            raise self._fail(self._map_status(status))
        answer, codecs = self.validate_answer(payload, response_headers.get("content-type"))
        location = response_headers.get("location")
        session_url = self.validate_location(location) if location is not None else None
        return WhepSession(
            status=status, answer_sdp=answer, session_url=session_url, video_codecs=codecs
        )

    def delete_session(self, session_url: str, bearer: SecretStr) -> None:
        """Bounded authenticated teardown of a validated session resource."""
        validated = self.validate_location(session_url)
        path = validated[len(f"https://{self.config.host}:{self.config.port}") :]
        status, _headers, _payload = self._request("DELETE", path, self._headers(bearer, {}), None)
        if not (200 <= status < 300 or status == 404):
            raise self._fail(_C.WHEP_TEARDOWN_FAILED)


class _suppress_close:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
