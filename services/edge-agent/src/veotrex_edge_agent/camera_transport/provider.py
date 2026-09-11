from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    NO_CREDENTIAL,
    RING_ENDPOINT_POLICY,
    CredentialMode,
    EndpointPolicy,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    SessionCredential,
    ValidatedEndpoint,
    VideoCodec,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory
from veotrex_edge_agent.qualification.backend import AccessTokenProvider
from veotrex_edge_agent.qualification.gstreamer import ring_rtsps_url
from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
    SessionRequest,
)

_AUTH_CATEGORIES = frozenset(
    {"reauth_required", "refresh_uncertain", "disconnected", "unauthorized", "forbidden"}
)


class LiveSessionProvider(Protocol):
    """Provider-side session acquisition. Raises TransportError with a taxonomy category only."""

    @property
    def kind(self) -> ProviderKind: ...

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease: ...


@dataclass(frozen=True, slots=True)
class SessionLifetime:
    lifetime_seconds: float
    renewal_lead_seconds: float | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.lifetime_seconds <= 86_400:
            raise ValueError("session lifetime is out of bounds")
        if self.renewal_lead_seconds is not None and not (
            0 < self.renewal_lead_seconds < self.lifetime_seconds
        ):
            raise ValueError("renewal lead must be positive and shorter than the lifetime")

    def bounds(self, now: float) -> tuple[float, float | None]:
        expires = now + self.lifetime_seconds
        renew = None if self.renewal_lead_seconds is None else expires - self.renewal_lead_seconds
        return expires, renew


CredentialSource = Callable[[], Awaitable[SessionCredential]]


class StaticRtspSessionProvider:
    """Generic RTSP(S) camera or the local fixture: one policy-validated endpoint plus an injected
    credential source. Optional lifetime emulates expiring provider sessions."""

    def __init__(
        self,
        kind: ProviderKind,
        endpoint: str,
        policy: EndpointPolicy,
        *,
        credential_source: CredentialSource | None = None,
        lifetime: SessionLifetime | None = None,
        codec_hint: VideoCodec | None = None,
    ) -> None:
        if kind is ProviderKind.RING:
            raise ValueError("Ring sessions must use RingLiveSessionProvider")
        self._kind = kind
        self._endpoint: ValidatedEndpoint = validate_endpoint(endpoint, policy)
        self._credential_source = credential_source
        self._lifetime = lifetime
        self._codec_hint = codec_hint

    @property
    def kind(self) -> ProviderKind:
        return self._kind

    @property
    def endpoint(self) -> ValidatedEndpoint:
        return self._endpoint

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease:
        credential = await self._credential_source() if self._credential_source else NO_CREDENTIAL
        expires, renew = self._lifetime.bounds(now) if self._lifetime else (None, None)
        descriptor = LiveSessionDescriptor(
            provider=self._kind,
            logical_camera_id=camera_id,
            generation=generation,
            endpoint=self._endpoint,
            created_monotonic=now,
            expires_monotonic=expires,
            renew_after_monotonic=renew,
            codec_hint=self._codec_hint,
            capabilities=frozenset({"LIVE_VIDEO"}),
        )
        return LiveSessionLease(descriptor, credential)


class RingLiveSessionProvider:
    """Ring Partner API RTSPS session source through the existing Stage 1D-A contract.

    It reuses the Stage 1D-A URL builder and the injected Stage 1B access-token boundary. The
    credential convention (token as RTSP password) is inherited from Stage 1D-A and has not been
    verified against a live Ring session. Without an injected token provider it fails closed with
    PROVIDER_NOT_CONFIGURED; there is no fallback credential path.
    """

    def __init__(
        self,
        target: CameraTarget,
        tokens: AccessTokenProvider | None,
        session_class: SessionClass,
        *,
        renewal_lead_seconds: float = 5.0,
    ) -> None:
        self._target = target
        self._tokens = tokens
        self._session_class = session_class
        self._lifetime = SessionLifetime(
            float(session_class.expected_limit_seconds), renewal_lead_seconds
        )

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.RING

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease:
        if self._tokens is None:
            raise TransportError(TransportErrorCategory.PROVIDER_NOT_CONFIGURED)
        if camera_id != self._target.camera_id:
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        try:
            raw = ring_rtsps_url(
                self._target.provider_device_id, self._target.provider_component_id
            )
        except ValueError:
            raise TransportError(TransportErrorCategory.INVALID_ENDPOINT) from None
        endpoint = validate_endpoint(raw, RING_ENDPOINT_POLICY)
        request = SessionRequest(
            self._target, QualificationMode.DECODE, self._session_class, generation
        )
        try:
            token = await self._tokens.access_token_for(request)
        except TransportError:
            raise
        except Exception as exc:
            category = getattr(exc, "category", None)
            if isinstance(category, str) and category.lower() in _AUTH_CATEGORIES:
                raise TransportError(TransportErrorCategory.AUTHORIZATION_FAILED) from None
            raise TransportError(TransportErrorCategory.PROVIDER_UNAVAILABLE) from None
        if not isinstance(token, SecretStr) or not token.get_secret_value():
            raise TransportError(TransportErrorCategory.AUTHORIZATION_FAILED)
        expires, renew = self._lifetime.bounds(now)
        descriptor = LiveSessionDescriptor(
            provider=ProviderKind.RING,
            logical_camera_id=camera_id,
            generation=generation,
            endpoint=endpoint,
            created_monotonic=now,
            expires_monotonic=expires,
            renew_after_monotonic=renew,
            capabilities=frozenset({"LIVE_VIDEO"}),
        )
        return LiveSessionLease(
            descriptor, SessionCredential(CredentialMode.RTSP_USER_PASSWORD, "x", token)
        )


FAKE_ENDPOINT = "rtsp://127.0.0.1:8554/synthetic-fake-camera"
FAKE_SECRET = "synthetic-fixture-secret-not-real"  # noqa: S105 - obviously synthetic


@dataclass(frozen=True, slots=True)
class FakeOutcome:
    category: TransportErrorCategory | None = None
    lifetime_seconds: float | None = None
    renewal_lead_seconds: float | None = None
    delay_seconds: float = 0.0


class FakeLiveSessionProvider:
    """Deterministic provider for the fake qualification matrix; no real secrets."""

    def __init__(
        self, outcomes: Iterable[FakeOutcome] = (), *, default: FakeOutcome | None = None
    ) -> None:
        self._outcomes = deque(outcomes)
        self._default = default or FakeOutcome()
        self.calls: list[int] = []

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.FAKE

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease:
        self.calls.append(generation)
        outcome = self._outcomes.popleft() if self._outcomes else self._default
        if outcome.delay_seconds:
            await asyncio.sleep(outcome.delay_seconds)
        if outcome.category is not None:
            raise TransportError(outcome.category)
        lifetime = (
            SessionLifetime(outcome.lifetime_seconds, outcome.renewal_lead_seconds)
            if outcome.lifetime_seconds
            else None
        )
        expires, renew = lifetime.bounds(now) if lifetime else (None, None)
        descriptor = LiveSessionDescriptor(
            provider=ProviderKind.FAKE,
            logical_camera_id=camera_id,
            generation=generation,
            endpoint=validate_endpoint(FAKE_ENDPOINT, LOCAL_FIXTURE_ENDPOINT_POLICY),
            created_monotonic=now,
            expires_monotonic=expires,
            renew_after_monotonic=renew,
        )
        credential = SessionCredential(
            CredentialMode.RTSP_USER_PASSWORD, "fixture", SecretStr(FAKE_SECRET)
        )
        return LiveSessionLease(descriptor, credential)
