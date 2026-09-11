"""Provider-neutral live camera transport (R5A).

CameraProvider -> LiveSessionDescriptor -> CameraTransportSession (controller + runner) ->
isolated GStreamer worker -> hardware decoder -> qualified decoded stream. Nothing here knows
about detection or tracking, and nothing downstream needs to know which provider is used.
"""

from veotrex_edge_agent.camera_transport.controller import ControllerConfig, TransportController
from veotrex_edge_agent.camera_transport.descriptor import (
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    SessionCredential,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory
from veotrex_edge_agent.camera_transport.state import TransportState

__all__ = [
    "ControllerConfig",
    "LiveSessionDescriptor",
    "LiveSessionLease",
    "ProviderKind",
    "SessionCredential",
    "TransportController",
    "TransportError",
    "TransportErrorCategory",
    "TransportState",
    "validate_endpoint",
]
