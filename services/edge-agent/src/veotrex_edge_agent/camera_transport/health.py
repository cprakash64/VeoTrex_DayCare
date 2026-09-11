from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class GapClass(StrEnum):
    NORMAL = "NORMAL"
    JITTER = "JITTER"
    DEGRADED = "DEGRADED"
    STALLED = "STALLED"


@dataclass(frozen=True, slots=True)
class StallPolicy:
    """Media-progress thresholds (seconds since the last decoded buffer).

    Initial qualification values: a 1 s gap is ordinary network/encoder jitter at 10-30 FPS,
    2 s is visible transport degradation, and 8 s with no media is a failed stream that is
    reconnected. They are deliberately loose to avoid reconnect loops from ordinary jitter.
    """

    jitter_tolerance_seconds: float = 1.0
    degraded_after_seconds: float = 2.0
    stalled_after_seconds: float = 8.0
    first_media_timeout_seconds: float = 15.0
    decoder_progress_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        values = (
            self.jitter_tolerance_seconds,
            self.degraded_after_seconds,
            self.stalled_after_seconds,
            self.first_media_timeout_seconds,
            self.decoder_progress_timeout_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("stall thresholds must be positive and finite")
        if not (
            self.jitter_tolerance_seconds
            < self.degraded_after_seconds
            < self.stalled_after_seconds
            <= 300
        ):
            raise ValueError("stall thresholds must satisfy jitter < degraded < stalled <= 300")
        if self.first_media_timeout_seconds > 300 or self.decoder_progress_timeout_seconds > 300:
            raise ValueError("startup timeouts must be at most 300 seconds")

    def classify(self, gap_seconds: float) -> GapClass:
        if gap_seconds >= self.stalled_after_seconds:
            return GapClass.STALLED
        if gap_seconds >= self.degraded_after_seconds:
            return GapClass.DEGRADED
        if gap_seconds >= self.jitter_tolerance_seconds:
            return GapClass.JITTER
        return GapClass.NORMAL


class DecoderState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    NEGOTIATING = "NEGOTIATING"
    DECODING = "DECODING"
    STALLED = "STALLED"
    FAILED = "FAILED"


class ProviderState(StrEnum):
    IDLE = "IDLE"
    ACQUIRING = "ACQUIRING"
    SESSION_AUTHORIZED = "SESSION_AUTHORIZED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"


@dataclass(frozen=True, slots=True)
class TransportHealth:
    """Layered health: each field answers a different question; none implies another."""

    logical_camera_id: str
    provider: str
    provider_state: str
    camera_available: bool | None
    session_authorized: bool
    transport_connected: bool
    media_flowing: bool
    decoder_healthy: bool
    transport_state: str
    decoder_state: str
    transport_protocol: str | None
    codec: str | None
    decoder: str | None
    hardware_decode: bool | None
    session_generation: int | None
    last_media_pts_ns: int | None
    last_media_monotonic: float | None
    recent_buffer_rate_fps: float | None
    recent_gap_seconds: float | None
    reconnect_count: int
    renewal_count: int
    stall_count: int
    session_expiry_remaining_seconds: float | None
    last_failure_category: str | None
    circuit_open: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
