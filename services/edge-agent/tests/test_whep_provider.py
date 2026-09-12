from uuid import UUID

import pytest
from pydantic import SecretStr

from veotrex_edge_agent.camera_transport.descriptor import (
    CredentialMode,
    ProviderKind,
    TransportProtocol,
)
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.webrtc_media import WebRtcRuntimeReport
from veotrex_edge_agent.camera_transport.whep_client import RING_WHEP_HOST
from veotrex_edge_agent.camera_transport.whep_provider import (
    RING_WHEP_ENDPOINT_POLICY,
    RingWhepSessionProvider,
)
from veotrex_edge_agent.qualification.models import CameraTarget, SessionRequest

CAMERA = UUID(int=0x5A)
TOKEN = "synthetic-ring-bearer-not-a-real-token"  # noqa: S105 - obviously synthetic
AVAILABLE = WebRtcRuntimeReport(True, (), None, "present")
UNAVAILABLE = WebRtcRuntimeReport(
    False, ("nicesrc", "nicesink"), "gstreamer1.0-nice", "libnice elements are unavailable"
)


class RecordingTokens:
    def __init__(self, token: str | None = TOKEN, error: Exception | None = None) -> None:
        self.calls = 0
        self._token = token
        self._error = error

    async def access_token_for(self, request: SessionRequest) -> SecretStr:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SecretStr(self._token or "")


def target(device: str = "synthetic-device-0001", component: str | None = None) -> CameraTarget:
    return CameraTarget(CAMERA, "camera-whep", device, component)


def provider(
    tokens: RecordingTokens | None, *, report: WebRtcRuntimeReport = AVAILABLE, **kwargs: object
) -> RingWhepSessionProvider:
    return RingWhepSessionProvider(
        target(**kwargs),  # type: ignore[arg-type]
        tokens,
        runtime_probe=lambda: report,
    )


async def test_missing_token_provider_fails_closed_before_anything_else() -> None:
    with pytest.raises(TransportError) as caught:
        await provider(None).acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.PROVIDER_NOT_CONFIGURED


async def test_unusable_webrtc_runtime_never_requests_a_ring_token() -> None:
    tokens = RecordingTokens()
    with pytest.raises(TransportError) as caught:
        await provider(tokens, report=UNAVAILABLE).acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.WEBRTC_RUNTIME_UNAVAILABLE
    assert tokens.calls == 0  # the credential boundary is never crossed on an unusable host


async def test_successful_acquire_yields_whep_descriptor_and_bearer_credential() -> None:
    tokens = RecordingTokens()
    lease = await provider(tokens).acquire(CAMERA, 3, 100.0)
    descriptor, credential = lease.descriptor, lease.credential
    assert descriptor.provider is ProviderKind.RING
    assert descriptor.transport_protocol is TransportProtocol.WHEP
    assert descriptor.generation == 3
    assert descriptor.endpoint.host == RING_WHEP_HOST
    assert descriptor.endpoint.port == 443
    assert descriptor.endpoint.uri.endswith("/media/streaming/whep/sessions")
    assert credential.mode is CredentialMode.BEARER
    assert credential.username == ""
    assert credential.secret.get_secret_value() == TOKEN
    # Ring documents no WHEP session lifetime, so none is invented.
    assert descriptor.expires_monotonic is None
    assert descriptor.renew_after_monotonic is None
    assert sorted(descriptor.capabilities) == ["LIVE_VIDEO", "WHEP"]
    assert tokens.calls == 1


async def test_no_token_appears_in_descriptor_repr_or_safe_dict() -> None:
    lease = await provider(RecordingTokens()).acquire(CAMERA, 1, 0.0)
    rendered = repr(lease) + str(lease) + repr(lease.descriptor) + str(lease.descriptor.safe_dict())
    rendered += repr(lease.credential) + str(lease.credential)
    assert TOKEN not in rendered
    assert "Bearer" not in lease.descriptor.endpoint.uri
    assert lease.descriptor.safe_dict()["transport_protocol"] == "WHEP"


def test_control_endpoint_uses_current_official_path_and_component_id() -> None:
    assert provider(None).control_endpoint_uri() == (
        f"https://{RING_WHEP_HOST}:443"
        "/v1/devices/synthetic-device-0001/media/streaming/whep/sessions"
    )
    with_component = provider(None, component="2").control_endpoint_uri()
    assert with_component.endswith("/media/streaming/whep/sessions?component_id=2")


def test_invalid_device_identifier_is_rejected_as_invalid_endpoint() -> None:
    with pytest.raises(TransportError) as caught:
        provider(None, device="../../admin").control_endpoint_uri()
    assert caught.value.category is C.INVALID_ENDPOINT


async def test_authorization_failures_map_to_whep_unauthorized() -> None:
    class LinkError(Exception):
        category = "reauth_required"

    tokens = RecordingTokens(error=LinkError())
    with pytest.raises(TransportError) as caught:
        await provider(tokens).acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.WHEP_HTTP_UNAUTHORIZED
    with pytest.raises(TransportError) as caught:
        await provider(RecordingTokens(error=RuntimeError("boom"))).acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.PROVIDER_UNAVAILABLE
    with pytest.raises(TransportError) as caught:
        await provider(RecordingTokens(token="")).acquire(CAMERA, 1, 0.0)
    assert caught.value.category is C.WHEP_HTTP_UNAUTHORIZED


async def test_camera_identity_mismatch_is_rejected() -> None:
    with pytest.raises(TransportError) as caught:
        await provider(RecordingTokens()).acquire(UUID(int=0x99), 1, 0.0)
    assert caught.value.category is C.INTERNAL_TRANSPORT_ERROR


def test_legacy_rtsps_provider_cannot_become_the_default_accidentally() -> None:
    from veotrex_edge_agent.camera_transport.provider import (
        RING_RTSPS_STATUS,
        RingLiveSessionProvider,
    )
    from veotrex_edge_agent.qualification.models import SessionClass

    assert RING_RTSPS_STATUS == "LEGACY_UNVERIFIED"
    # Constructing the unverified RTSPS path without acknowledgement is refused.
    with pytest.raises(TransportError) as caught:
        RingLiveSessionProvider(target(), None, SessionClass.LINE_POWERED_60_SECONDS)
    assert caught.value.category is C.TRANSPORT_PROTOCOL_UNSUPPORTED
    explicit = RingLiveSessionProvider(
        target(), None, SessionClass.LINE_POWERED_60_SECONDS, acknowledge_unverified=True
    )
    assert explicit.kind is ProviderKind.RING


def test_whep_endpoint_policy_pins_scheme_host_and_port() -> None:
    policy = RING_WHEP_ENDPOINT_POLICY
    assert policy.allowed_schemes == frozenset({"https"})
    assert policy.allowed_hosts == frozenset({RING_WHEP_HOST})
    assert policy.allowed_ports == frozenset({443})
    assert policy.allow_loopback is False and policy.allow_private is False
