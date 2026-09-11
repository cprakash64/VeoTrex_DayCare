from __future__ import annotations

from enum import StrEnum


class TransportErrorCategory(StrEnum):
    """Deterministic, credential-free transport failure taxonomy."""

    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    SESSION_ACQUIRE_TIMEOUT = "SESSION_ACQUIRE_TIMEOUT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    TRANSPORT_CONNECT_FAILED = "TRANSPORT_CONNECT_FAILED"
    TRANSPORT_DISCONNECTED = "TRANSPORT_DISCONNECTED"
    TRANSPORT_PROTOCOL_UNSUPPORTED = "TRANSPORT_PROTOCOL_UNSUPPORTED"
    REDIRECT_REFUSED = "REDIRECT_REFUSED"
    INVALID_ENDPOINT = "INVALID_ENDPOINT"
    CODEC_UNSUPPORTED = "CODEC_UNSUPPORTED"
    DECODER_START_FAILED = "DECODER_START_FAILED"
    DECODER_FAILED = "DECODER_FAILED"
    FIRST_MEDIA_TIMEOUT = "FIRST_MEDIA_TIMEOUT"
    MEDIA_STALLED = "MEDIA_STALLED"
    END_OF_STREAM = "END_OF_STREAM"
    TIMESTAMP_DISCONTINUITY = "TIMESTAMP_DISCONTINUITY"
    RENEWAL_FAILED = "RENEWAL_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    WORKER_EXITED = "WORKER_EXITED"
    INTERNAL_TRANSPORT_ERROR = "INTERNAL_TRANSPORT_ERROR"


# Retrying these cannot succeed without operator/provider action; retrying authorization failures
# would also hammer the provider and could trigger account lockout.
TERMINAL_CATEGORIES = frozenset(
    {
        TransportErrorCategory.AUTHORIZATION_FAILED,
        TransportErrorCategory.TRANSPORT_PROTOCOL_UNSUPPORTED,
        TransportErrorCategory.REDIRECT_REFUSED,
        TransportErrorCategory.INVALID_ENDPOINT,
        TransportErrorCategory.CODEC_UNSUPPORTED,
        TransportErrorCategory.PROVIDER_NOT_CONFIGURED,
    }
)


def safe_category(value: object) -> TransportErrorCategory:
    """Map untrusted input (worker/provider) onto the fixed taxonomy without echoing it."""
    if isinstance(value, TransportErrorCategory):
        return value
    if isinstance(value, str):
        try:
            return TransportErrorCategory(value)
        except ValueError:
            pass
    return TransportErrorCategory.INTERNAL_TRANSPORT_ERROR


class TransportError(Exception):
    """Carries only a taxonomy category; never provider text, URLs, or credentials."""

    def __init__(self, category: TransportErrorCategory) -> None:
        super().__init__(category.value)
        self.category = category
