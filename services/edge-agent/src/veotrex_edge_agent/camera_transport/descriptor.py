from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory

MAX_ENDPOINT_LENGTH = 2048
MAX_SESSION_LIFETIME_SECONDS = 86_400.0
_DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322}
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PATH = re.compile(r"^(/[A-Za-z0-9._~%=+,:-]*)*$")
_QUERY = re.compile(r"^[A-Za-z0-9._~%=&+,:-]*$")
_PUBLIC_DIAGNOSTIC_HOSTS = frozenset({"video.rtsp.amazonvision.com"})


class ProviderKind(StrEnum):
    RING = "RING"
    RTSP = "RTSP"
    LOCAL_FIXTURE = "LOCAL_FIXTURE"
    FAKE = "FAKE"


class TransportProtocol(StrEnum):
    RTSP = "RTSP"
    RTSPS = "RTSPS"


class VideoCodec(StrEnum):
    H264 = "H264"
    H265 = "H265"


class CredentialMode(StrEnum):
    NONE = "NONE"
    RTSP_USER_PASSWORD = "RTSP_USER_PASSWORD"  # noqa: S105 - mode name, not a credential


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    """Provider contract for what a session endpoint may point at. Deny by default."""

    name: str
    allowed_schemes: frozenset[str]
    allowed_hosts: frozenset[str] | None = None
    allowed_ports: frozenset[int] | None = None
    path_pattern: re.Pattern[str] | None = None
    query_pattern: re.Pattern[str] | None = None
    allow_loopback: bool = False
    allow_private: bool = False
    allow_dns_names: bool = True


RING_ENDPOINT_POLICY = EndpointPolicy(
    name="ring-rtsps",
    allowed_schemes=frozenset({"rtsps"}),
    allowed_hosts=frozenset({"video.rtsp.amazonvision.com"}),
    allowed_ports=frozenset({322}),
    path_pattern=re.compile(r"^/v1/devices/[A-Za-z0-9._~%-]{1,512}/stream$"),
    query_pattern=re.compile(r"^(component_id=[A-Za-z0-9._~%-]{1,512})?$"),
)
LOCAL_FIXTURE_ENDPOINT_POLICY = EndpointPolicy(
    name="local-fixture",
    allowed_schemes=frozenset({"rtsp"}),
    allowed_hosts=frozenset({"127.0.0.1"}),
    allow_loopback=True,
    allow_dns_names=False,
)
# Site cameras live on private LANs; loopback, link-local (cloud metadata), multicast, and
# unspecified addresses are never valid camera targets.
GENERIC_RTSP_ENDPOINT_POLICY = EndpointPolicy(
    name="generic-rtsp",
    allowed_schemes=frozenset({"rtsp", "rtsps"}),
    allow_private=True,
)


def _invalid() -> TransportError:
    return TransportError(TransportErrorCategory.INVALID_ENDPOINT)


def _host_class(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "dns"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    return "public"


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedEndpoint:
    """Credential-free transport target that passed an EndpointPolicy."""

    scheme: str
    host: str
    port: int
    path: str
    query: str
    policy_name: str

    @property
    def protocol(self) -> TransportProtocol:
        return TransportProtocol.RTSPS if self.scheme == "rtsps" else TransportProtocol.RTSP

    @property
    def uri(self) -> str:
        """Full target. Worker/pipeline use only; never log, label, or report it."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        suffix = f"?{self.query}" if self.query else ""
        return f"{self.scheme}://{host}:{self.port}{self.path}{suffix}"

    def safe_repr(self) -> str:
        host = self.host if self.host in _PUBLIC_DIAGNOSTIC_HOSTS else _host_class(self.host)
        return f"{self.scheme}://{host}:{self.port}/REDACTED"

    def __repr__(self) -> str:
        return f"ValidatedEndpoint({self.safe_repr()})"

    __str__ = __repr__


def validate_endpoint(raw: object, policy: EndpointPolicy) -> ValidatedEndpoint:
    """Reject anything but a policy-conformant RTSP(S) URL. Errors never echo the input."""
    if not isinstance(raw, str) or not 0 < len(raw) <= MAX_ENDPOINT_LENGTH:
        raise _invalid()
    # Printable ASCII only: no whitespace, control characters, or Unicode confusables, which also
    # prevents any value from resembling a gst-launch pipeline fragment.
    if any(not 33 <= ord(character) <= 126 for character in raw):
        raise _invalid()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _invalid() from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or scheme not in policy.allowed_schemes:
        raise _invalid()
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise _invalid()  # Credentials travel separately, never inside the URL.
    if parts.fragment or "#" in raw:
        raise _invalid()
    host = (parts.hostname or "").lower()
    if not host:
        raise _invalid()
    port = port if port is not None else _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65_535:
        raise _invalid()
    path = parts.path or "/"
    if not _PATH.fullmatch(path) or any(segment in {".", ".."} for segment in path.split("/")):
        raise _invalid()
    if "%2e" in path.lower() and ".." in path.lower().replace("%2e", "."):
        raise _invalid()
    query = parts.query
    if not _QUERY.fullmatch(query):
        raise _invalid()
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        labels = host.rstrip(".").split(".")
        if not policy.allow_dns_names or host in {"localhost", "localhost."}:
            raise _invalid()
        if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
            raise _invalid()
        if host.endswith((".localhost", ".local.")):
            raise _invalid()
    else:
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise _invalid()
        if address.is_reserved and not address.is_private:
            raise _invalid()
        if address.is_loopback and not policy.allow_loopback:
            raise _invalid()
        if (
            address.is_private
            and not address.is_loopback
            and not policy.allow_private
            and not (policy.allowed_hosts and host in policy.allowed_hosts)
        ):
            raise _invalid()
    if policy.allowed_hosts is not None and host not in policy.allowed_hosts:
        raise _invalid()
    if policy.allowed_ports is not None and port not in policy.allowed_ports:
        raise _invalid()
    if policy.path_pattern is not None and not policy.path_pattern.fullmatch(path):
        raise _invalid()
    if policy.query_pattern is not None and not policy.query_pattern.fullmatch(query):
        raise _invalid()
    return ValidatedEndpoint(scheme, host, port, path, query, policy.name)


@dataclass(frozen=True, slots=True, repr=False)
class SessionCredential:
    """Transport secret, kept apart from the descriptor and never serialized."""

    mode: CredentialMode
    username: str = ""
    secret: SecretStr = field(default_factory=lambda: SecretStr(""))

    def __post_init__(self) -> None:
        if self.mode is CredentialMode.RTSP_USER_PASSWORD:
            if not self.username or not self.secret.get_secret_value():
                raise TransportError(TransportErrorCategory.PROVIDER_NOT_CONFIGURED)
            if any(
                not 32 < ord(character) < 127 or character == ":" for character in self.username
            ):
                raise TransportError(TransportErrorCategory.PROVIDER_NOT_CONFIGURED)

    def __repr__(self) -> str:
        return f"SessionCredential(mode={self.mode.value}, secret=**********)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("session credentials are not serializable")


NO_CREDENTIAL = SessionCredential(CredentialMode.NONE)


@dataclass(frozen=True, slots=True, repr=False)
class LiveSessionDescriptor:
    """Immutable, secret-free description of one provider live session (one generation).

    Times are monotonic seconds. Providers that report relative lifetimes are converted at
    acquisition so wall-clock and monotonic arithmetic are never mixed.
    """

    provider: ProviderKind
    logical_camera_id: UUID
    generation: int
    endpoint: ValidatedEndpoint
    created_monotonic: float
    expires_monotonic: float | None = None
    renew_after_monotonic: float | None = None
    codec_hint: VideoCodec | None = None
    capabilities: frozenset[str] = frozenset()
    session_ref: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("session generation must be an integer")
        if self.generation < 1:
            raise ValueError("session generation must be positive")
        if self.expires_monotonic is not None:
            lifetime = self.expires_monotonic - self.created_monotonic
            if not 0 < lifetime <= MAX_SESSION_LIFETIME_SECONDS:
                raise ValueError("session lifetime is out of bounds")
        if self.renew_after_monotonic is not None:
            if self.expires_monotonic is None:
                raise ValueError("renewal requires an expiring session")
            if not (self.created_monotonic <= self.renew_after_monotonic < self.expires_monotonic):
                raise ValueError("renewal time must precede expiry")
        if len(self.capabilities) > 16 or any(
            not re.fullmatch(r"[A-Z0-9_]{1,40}", value) for value in self.capabilities
        ):
            raise ValueError("capability metadata is not bounded")

    @property
    def transport_protocol(self) -> TransportProtocol:
        return self.endpoint.protocol

    def expired(self, now: float) -> bool:
        return self.expires_monotonic is not None and now >= self.expires_monotonic

    def expiry_remaining(self, now: float) -> float | None:
        if self.expires_monotonic is None:
            return None
        return max(0.0, self.expires_monotonic - now)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "logical_camera_id": str(self.logical_camera_id),
            "generation": self.generation,
            "transport_protocol": self.transport_protocol.value,
            "endpoint": self.endpoint.safe_repr(),
            "codec_hint": self.codec_hint.value if self.codec_hint else None,
            "lifetime_seconds": (
                None
                if self.expires_monotonic is None
                else self.expires_monotonic - self.created_monotonic
            ),
            "renewal_lead_seconds": (
                None
                if self.renew_after_monotonic is None or self.expires_monotonic is None
                else self.expires_monotonic - self.renew_after_monotonic
            ),
            "capabilities": sorted(self.capabilities),
        }

    def __repr__(self) -> str:
        return (
            f"LiveSessionDescriptor(provider={self.provider.value}, "
            f"camera={self.logical_camera_id}, generation={self.generation}, "
            f"endpoint={self.endpoint.safe_repr()})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class LiveSessionLease:
    """A descriptor plus its separately held credential, handed only to the media backend."""

    descriptor: LiveSessionDescriptor
    credential: SessionCredential

    def __repr__(self) -> str:
        return f"LiveSessionLease({self.descriptor!r}, credential=**********)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("session leases are not serializable")


_URI_USERINFO = re.compile(r"(?i)\b(rtsps?|https?|wss?)://[^/\s@]*@")
_URI_REST = re.compile(r"(?i)\b(rtsps?)://([^/\s?#]+)[^\s\"']*")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}")
_KEYED = re.compile(
    r"(?i)\b(access_token|refresh_token|token|password|passwd|user-pw|secret|code|cookie)"
    r"(\s*[=:]\s*)[^\s,;&\"']+"
)


def redact_text(text: str) -> str:
    """Last-line scrub for diagnostics: userinfo, RTSP paths, bearer/basic and key=value secrets."""
    value = _URI_USERINFO.sub(lambda match: f"{match.group(1)}://REDACTED@", text)
    value = _URI_REST.sub(lambda match: f"{match.group(1)}://{match.group(2)}/REDACTED", value)
    value = _BEARER.sub(lambda match: f"{match.group(1)} REDACTED", value)
    return _KEYED.sub(lambda match: f"{match.group(1)}{match.group(2)}REDACTED", value)
