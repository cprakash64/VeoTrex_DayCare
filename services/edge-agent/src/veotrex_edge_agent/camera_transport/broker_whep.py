"""Brokered WHEP: the existing WHEP stack, pointed at the VeoTrex control plane (V1-DEMO-03B).

The Jetson never holds a Ring OAuth token. Instead of calling Ring's WHEP endpoint with a Ring
bearer, it calls the control plane's WHEP-compatible broker with its own machine credential:

    POST https://<control plane>/v1/edge/cameras/<VeoTrex camera UUID>/whep
    Authorization: Bearer <edge machine credential>
    Content-Type: application/sdp            (the WebRTC worker's offer)

    201 Created, Content-Type: application/sdp (Ring's answer, relayed)
    Location: /v1/edge/whep-leases/<opaque>   (a VeoTrex lease, never Ring's session URL)

    DELETE https://<control plane>/v1/edge/whep-leases/<opaque>

That exchange has exactly the WHEP shape ``WhepClient`` already speaks, so nothing new is built
here: ``BrokerWhepClient`` is ``WhepClient`` with a different origin, a different session path
and a different Location rule, and it inherits the HTTP handling, bounds, TLS verification,
redirect refusal, SDP answer validation and status taxonomy unchanged. The camera is named by
its VeoTrex UUID; Ring device and component ids never reach the edge.

The machine credential is read from a protected file at the moment it is needed - never from an
argument, an environment variable or a log - and exists only in a request header.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.descriptor import (
    CredentialMode,
    EndpointPolicy,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    SessionCredential,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory
from veotrex_edge_agent.camera_transport.webrtc_media import (
    WebRtcRuntimeReport,
    probe_webrtc_runtime,
)
from veotrex_edge_agent.camera_transport.whep_client import (
    ConnectionFactory,
    WhepClient,
    WhepConfig,
    _default_connection,
)

_C = TransportErrorCategory
# Environments where the control plane may be a loopback or private address (a developer's
# API or a test fixture). Everywhere else it must be a public DNS name over HTTPS.
LOCAL_ENVIRONMENTS = frozenset({"local", "test"})
EDGE_CREDENTIAL_MAX_BYTES = 256
_EDGE_TOKEN = re.compile(
    r"^vte1\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$"
)
_CAMERA_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_OFFER_PATH = re.compile(r"^/v1/edge/cameras/[0-9a-f-]{36}/whep$")
_LEASE_PATH = re.compile(r"^/v1/edge/whep-leases/[A-Za-z0-9_-]{43}$")
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


# --------------------------------------------------------------------------- credential file
class EdgeCredentialError(Exception):
    """The machine credential file is unusable. ``reason`` is fixed text; never the content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read_edge_credential(path: str | os.PathLike[str]) -> SecretStr:
    """Read the edge machine credential from a protected regular file.

    Refused: an unset or relative path, a symlink (not followed), anything but a regular file,
    a file group/other can access, a file owned by another user, an empty, oversized or
    non-ASCII file, and content that is not exactly one well-formed credential. One trailing
    newline is tolerated because the admin tool writes one.
    """
    raw = os.fspath(path) if path else ""
    if not raw:
        raise EdgeCredentialError("credential_file_not_configured")
    if not os.path.isabs(raw):
        raise EdgeCredentialError("credential_file_path_not_absolute")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)  # a FIFO must not block the caller
    )
    try:
        descriptor = os.open(raw, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise EdgeCredentialError("credential_file_is_symlink") from None
        if exc.errno == errno.ENOENT:
            raise EdgeCredentialError("credential_file_missing") from None
        raise EdgeCredentialError("credential_file_unreadable") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise EdgeCredentialError("credential_file_not_regular")
        if status.st_mode & 0o077:
            raise EdgeCredentialError("credential_file_permissions_too_open")
        if status.st_uid != os.geteuid():
            raise EdgeCredentialError("credential_file_wrong_owner")
        if status.st_size > EDGE_CREDENTIAL_MAX_BYTES:
            raise EdgeCredentialError("credential_file_too_large")
        content = os.read(descriptor, EDGE_CREDENTIAL_MAX_BYTES + 1)
    except OSError:
        raise EdgeCredentialError("credential_file_unreadable") from None
    finally:
        os.close(descriptor)
    if len(content) > EDGE_CREDENTIAL_MAX_BYTES:
        raise EdgeCredentialError("credential_file_too_large")
    if content.endswith(b"\r\n"):
        content = content[:-2]
    elif content.endswith(b"\n"):
        content = content[:-1]
    if not content:
        raise EdgeCredentialError("credential_file_empty")
    try:
        value = content.decode("ascii", errors="strict")
    except UnicodeError:
        raise EdgeCredentialError("credential_malformed") from None
    if not _EDGE_TOKEN.fullmatch(value):
        raise EdgeCredentialError("credential_malformed")
    return SecretStr(value)


# ------------------------------------------------------------------------ control plane URL
@dataclass(frozen=True, slots=True)
class ControlPlaneEndpoint:
    """The broker origin: always HTTPS, never a path, never credentials in the URL."""

    host: str
    port: int
    local: bool = False

    @classmethod
    def parse(cls, url: str, *, environment: str) -> ControlPlaneEndpoint:
        local = environment in LOCAL_ENVIRONMENTS
        if not isinstance(url, str) or not 0 < len(url) <= 256:
            raise ValueError("control plane URL is required")
        if any(not 33 <= ord(character) <= 126 for character in url):
            raise ValueError("control plane URL must be printable ASCII without spaces")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError("control plane URL must use https")
        if "@" in parts.netloc or parts.username or parts.password:
            raise ValueError("control plane URL must not carry credentials")
        if parts.path not in {"", "/"} or parts.query or parts.fragment or "#" in url:
            raise ValueError("control plane URL must be a bare origin")
        try:
            port = parts.port or 443
        except ValueError:
            raise ValueError("control plane URL has an invalid port") from None
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("control plane URL must name a host")
        try:
            address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
                host
            )
        except ValueError:
            address = None
        if address is None:
            labels = host.rstrip(".").split(".")
            if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
                raise ValueError("control plane host is not a valid DNS name")
            if host == "localhost" or host.endswith((".localhost", ".local")):
                raise ValueError("control plane host must not be a local name")
        elif address.version != 4:
            raise ValueError("control plane address must be IPv4 or a DNS name")
        elif not local and not address.is_global:
            raise ValueError("a loopback or private control plane is allowed only locally")
        elif address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError("control plane address is not routable")
        return cls(host, port, local)

    @property
    def origin(self) -> str:
        return f"https://{self.host}:{self.port}"

    def offer_uri(self, camera_id: UUID) -> str:
        return f"{self.origin}/v1/edge/cameras/{camera_id}/whep"

    def endpoint_policy(self) -> EndpointPolicy:
        return EndpointPolicy(
            name="veotrex-whep-broker",
            allowed_schemes=frozenset({"https"}),
            allowed_hosts=frozenset({self.host}),
            allowed_ports=frozenset({self.port}),
            path_pattern=_OFFER_PATH,
            query_pattern=re.compile(r"^$"),
            allow_loopback=self.local,
            allow_private=self.local,
        )


# ------------------------------------------------------------------------------ WHEP client
class BrokerWhepClient(WhepClient):
    """``WhepClient`` for the VeoTrex broker. The bearer it sends is the EDGE credential.

    Only three things differ from the Ring client: the origin (from ``ControlPlaneEndpoint``),
    the session path (a VeoTrex camera UUID, no component), and the Location rule (an opaque
    VeoTrex lease on that same origin). A Ring-shaped Location is refused like any other.
    """

    def __init__(
        self,
        endpoint: ControlPlaneEndpoint,
        *,
        connection_factory: ConnectionFactory = _default_connection,
        clock: Callable[[], float] = time.monotonic,
        connect_timeout_seconds: float = 5.0,
        total_timeout_seconds: float = 20.0,
    ) -> None:
        super().__init__(
            WhepConfig(
                host=endpoint.host,
                port=endpoint.port,
                connect_timeout_seconds=connect_timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
                user_agent="VeoTrex-Edge/1.0 (broker)",
            ),
            connection_factory=connection_factory,
            clock=clock,
        )
        self.endpoint = endpoint

    def session_path(self, device_id: str, component_id: str | None = None) -> str:
        # ``device_id`` is the VeoTrex camera UUID here; there is no provider component.
        if component_id is not None:
            raise self._fail(_C.INVALID_ENDPOINT)
        if not isinstance(device_id, str) or not _CAMERA_UUID.fullmatch(device_id):
            raise self._fail(_C.INVALID_ENDPOINT)
        return f"/v1/edge/cameras/{device_id}/whep"

    def validate_location(self, raw: object) -> str:
        if not isinstance(raw, str) or not 0 < len(raw) <= 256:
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
            if parts.scheme != "https" or "@" in parts.netloc:
                raise self._fail(_C.WHEP_INVALID_LOCATION)
            if (parts.hostname or "").lower() != self.config.host or port != self.config.port:
                raise self._fail(_C.WHEP_INVALID_LOCATION)
            path, query = parts.path, parts.query
        if query or not _LEASE_PATH.fullmatch(path):
            raise self._fail(_C.WHEP_INVALID_LOCATION)
        return f"https://{self.config.host}:{self.config.port}{path}"


# --------------------------------------------------------------------- session provider
class BrokeredWhepSessionProvider:
    """``LiveSessionProvider`` whose lease points at the broker and carries the edge credential.

    Same fail-closed order as ``RingWhepSessionProvider``: an unusable WebRTC runtime is
    detected before the credential file is even opened. No ``AccessTokenProvider`` exists on
    this path, so there is nothing that could fetch a Ring token.
    """

    def __init__(
        self,
        camera_id: UUID,
        endpoint: ControlPlaneEndpoint,
        credential_file: str | os.PathLike[str],
        *,
        runtime_probe: Callable[[], WebRtcRuntimeReport] = probe_webrtc_runtime,
        credential_reader: Callable[[str | os.PathLike[str]], SecretStr] = read_edge_credential,
    ) -> None:
        self._camera_id = camera_id
        self._endpoint = endpoint
        self._credential_file = credential_file
        self._probe = runtime_probe
        self._read = credential_reader

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.RING

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease:
        if camera_id != self._camera_id:
            raise TransportError(_C.INTERNAL_TRANSPORT_ERROR)
        if not self._probe().available:
            raise TransportError(_C.WEBRTC_RUNTIME_UNAVAILABLE)
        endpoint = validate_endpoint(
            self._endpoint.offer_uri(camera_id), self._endpoint.endpoint_policy()
        )
        try:
            credential = self._read(self._credential_file)
        except EdgeCredentialError:
            raise TransportError(_C.PROVIDER_NOT_CONFIGURED) from None
        descriptor = LiveSessionDescriptor(
            provider=ProviderKind.RING,
            logical_camera_id=camera_id,
            generation=generation,
            endpoint=endpoint,
            created_monotonic=now,
            capabilities=frozenset({"LIVE_VIDEO", "WHEP", "BROKERED"}),
        )
        return LiveSessionLease(
            descriptor, SessionCredential(CredentialMode.BEARER, "", credential)
        )

    def __repr__(self) -> str:
        return f"BrokeredWhepSessionProvider(camera_id={self._camera_id}, credential=REDACTED)"


# ------------------------------------------------------------------------- answer exchange
@dataclass(frozen=True, slots=True, repr=False)
class _HeldLease:
    url: str
    credential: SecretStr


class BrokeredWhepExchange:
    """``AnswerExchange`` for ``WebRtcMediaBackend``: offer in, broker answer out.

    Holds the opaque lease URL of each generation so ``release`` can DELETE it. Refuses any
    lease that does not point at this client's broker origin, so the edge credential is never
    presented anywhere else - including to Ring.
    """

    def __init__(self, client: BrokerWhepClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._held: dict[int, _HeldLease] = {}

    def __call__(self, offer_sdp: str, lease: LiveSessionLease) -> str:
        descriptor = lease.descriptor
        endpoint = descriptor.endpoint
        camera = str(descriptor.logical_camera_id)
        if (
            lease.credential.mode is not CredentialMode.BEARER
            or endpoint.scheme != "https"
            or endpoint.host != self._client.config.host
            or endpoint.port != self._client.config.port
            or endpoint.path != self._client.session_path(camera)
            or endpoint.query
        ):
            raise TransportError(_C.INVALID_ENDPOINT)
        session = self._client.create_session(camera, lease.credential.secret, offer_sdp)
        if session.session_url is None:
            # The broker always issues a lease; a missing one is a contract violation.
            raise TransportError(_C.WHEP_INVALID_LOCATION)
        with self._lock:
            previous = self._held.pop(descriptor.generation, None)
            self._held[descriptor.generation] = _HeldLease(
                session.session_url, lease.credential.secret
            )
        if previous is not None:  # pragma: no cover - a generation negotiates once
            with contextlib.suppress(TransportError):
                self._client.delete_session(previous.url, previous.credential)
        return session.answer_sdp

    def release(self, generation: int) -> bool:
        """DELETE the generation's lease. Idempotent; never raises."""
        with self._lock:
            held = self._held.pop(generation, None)
        if held is None:
            return False
        try:
            self._client.delete_session(held.url, held.credential)
        except TransportError:
            return False
        return True

    def release_all(self) -> int:
        with self._lock:
            generations = list(self._held)
        return sum(1 for generation in generations if self.release(generation))

    @property
    def held(self) -> int:
        with self._lock:
            return len(self._held)

    def __repr__(self) -> str:
        return f"BrokeredWhepExchange(held={self.held}, credential=REDACTED)"


def broker_from_settings(settings: Any) -> tuple[ControlPlaneEndpoint, Path]:
    """Resolve the broker origin and credential file from ``EdgeSettings``, or refuse."""
    url = getattr(settings, "control_plane_url", "") or ""
    credential_file = getattr(settings, "credential_file", "") or ""
    if not url or not credential_file:
        raise TransportError(_C.PROVIDER_NOT_CONFIGURED)
    try:
        endpoint = ControlPlaneEndpoint.parse(url, environment=settings.environment)
    except ValueError:
        raise TransportError(_C.PROVIDER_NOT_CONFIGURED) from None
    return endpoint, Path(credential_file)
