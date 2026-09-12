"""Official Ring WHEP live-session provider.

Reuses the R5A provider-neutral contract: it yields a `LiveSessionLease` whose descriptor points
at the Ring WHEP **control** endpoint and whose credential is a Bearer token held separately.
Media negotiation belongs to the WebRTC media backend, so this provider deliberately performs no
SDP work and no HTTP request of its own.

Fail-closed order matters: an unusable WebRTC runtime is detected **before** any token is
requested, so a host that cannot play WHEP media never touches a Ring credential.
"""

from __future__ import annotations

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
    RING_WHEP_HOST,
    RING_WHEP_PORT,
    WhepClient,
)
from veotrex_edge_agent.qualification.backend import AccessTokenProvider
from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
    SessionRequest,
)

# Path shape is enforced by WhepClient.session_path; the policy pins scheme, host, and port.
RING_WHEP_ENDPOINT_POLICY = EndpointPolicy(
    name="ring-whep",
    allowed_schemes=frozenset({"https"}),
    allowed_hosts=frozenset({RING_WHEP_HOST}),
    allowed_ports=frozenset({RING_WHEP_PORT}),
)
_AUTH_CATEGORIES = frozenset(
    {"reauth_required", "refresh_uncertain", "disconnected", "unauthorized", "forbidden"}
)


class RingWhepSessionProvider:
    """Acquires an authorized Ring WHEP session context for one logical camera.

    Ring does not currently document a WHEP session lifetime, so no expiry or renewal deadline is
    invented: the descriptor carries no expiry and the controller therefore never schedules a
    renewal it cannot justify.
    """

    def __init__(
        self,
        target: CameraTarget,
        tokens: AccessTokenProvider | None,
        *,
        client: WhepClient | None = None,
        runtime_probe: object = probe_webrtc_runtime,
    ) -> None:
        self._target = target
        self._tokens = tokens
        self._client = client or WhepClient()
        self._probe = runtime_probe

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.RING

    def runtime_report(self) -> WebRtcRuntimeReport:
        probe = self._probe
        assert callable(probe)
        report = probe()
        assert isinstance(report, WebRtcRuntimeReport)
        return report

    def control_endpoint_uri(self) -> str:
        path = self._client.session_path(
            self._target.provider_device_id, self._target.provider_component_id
        )
        return f"https://{RING_WHEP_HOST}:{RING_WHEP_PORT}{path}"

    async def acquire(self, camera_id: UUID, generation: int, now: float) -> LiveSessionLease:
        if self._tokens is None:
            raise TransportError(TransportErrorCategory.PROVIDER_NOT_CONFIGURED)
        if camera_id != self._target.camera_id:
            raise TransportError(TransportErrorCategory.INTERNAL_TRANSPORT_ERROR)
        # Checked before the credential so an unusable host never requests a Ring token.
        if not self.runtime_report().available:
            raise TransportError(TransportErrorCategory.WEBRTC_RUNTIME_UNAVAILABLE)
        endpoint = validate_endpoint(self.control_endpoint_uri(), RING_WHEP_ENDPOINT_POLICY)
        request = SessionRequest(
            self._target,
            QualificationMode.DECODE,
            SessionClass.LINE_POWERED_60_SECONDS,
            generation,
        )
        try:
            token = await self._tokens.access_token_for(request)
        except TransportError:
            raise
        except Exception as exc:
            category = getattr(exc, "category", None)
            if isinstance(category, str) and category.lower() in _AUTH_CATEGORIES:
                raise TransportError(TransportErrorCategory.WHEP_HTTP_UNAUTHORIZED) from None
            raise TransportError(TransportErrorCategory.PROVIDER_UNAVAILABLE) from None
        if not isinstance(token, SecretStr) or not token.get_secret_value():
            raise TransportError(TransportErrorCategory.WHEP_HTTP_UNAUTHORIZED)
        descriptor = LiveSessionDescriptor(
            provider=ProviderKind.RING,
            logical_camera_id=camera_id,
            generation=generation,
            endpoint=endpoint,
            created_monotonic=now,
            capabilities=frozenset({"LIVE_VIDEO", "WHEP"}),
        )
        return LiveSessionLease(descriptor, SessionCredential(CredentialMode.BEARER, "", token))
