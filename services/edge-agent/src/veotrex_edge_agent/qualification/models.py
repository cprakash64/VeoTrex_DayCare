from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class QualificationMode(StrEnum):
    TRANSPORT = "transport"
    DECODE = "decode"


class SessionClass(StrEnum):
    BATTERY_30_SECONDS = "battery_30_seconds"
    LINE_POWERED_60_SECONDS = "line_powered_60_seconds"

    @property
    def expected_limit_seconds(self) -> int:
        return 30 if self is self.BATTERY_30_SECONDS else 60


class TerminationReason(StrEnum):
    PROVIDER_SESSION_EXPIRATION = "provider_session_expiration"
    OPERATOR_DURATION_REACHED = "operator_duration_reached"
    ACCESS_TOKEN_FAILURE = "access_token_failure"  # noqa: S105 - safe failure taxonomy
    NETWORK_DISCONNECT = "network_disconnect"
    TLS_FAILURE = "tls_failure"
    CODEC_NEGOTIATION_FAILURE = "codec_negotiation_failure"
    CAMERA_OFFLINE = "camera_offline"
    NO_FRAME_STALL = "no_frame_stall"
    DECODER_FAILURE = "decoder_failure"
    CONCURRENT_SESSION_REJECTED = "concurrent_session_rejected"
    APPLICATION_ERROR = "application_error"
    CANCELLED = "cancelled"
    END_OF_STREAM = "end_of_stream"


class QualificationDecision(StrEnum):
    MEETS_STREAM_TARGET = "MEETS_STREAM_TARGET"
    CONDITIONAL = "CONDITIONAL"
    DOES_NOT_MEET_STREAM_TARGET = "DOES_NOT_MEET_STREAM_TARGET"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class CameraTarget:
    camera_id: UUID
    label: str
    provider_device_id: str = field(repr=False)
    provider_component_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SessionRequest:
    target: CameraTarget
    mode: QualificationMode
    session_class: SessionClass
    session_number: int
    decoder_preference: str = "auto"
    stall_timeout_seconds: float = 5.0


@dataclass(slots=True)
class SessionResult:
    session_number: int
    requested_at: float
    connection_started_at: float | None = None
    describe_completed_at: float | None = None
    play_started_at: float | None = None
    first_media_at: float | None = None
    first_decoded_frame_at: float | None = None
    last_media_at: float | None = None
    last_decoded_frame_at: float | None = None
    ended_at: float | None = None
    teardown_completed_at: float | None = None
    codec: str | None = None
    audio_codec: str | None = None
    audio_present: bool = False
    decoder: str | None = None
    width: int | None = None
    height: int | None = None
    media_buffers: int = 0
    decoded_frames: int = 0
    encoded_bytes: int = 0
    audio_buffers: int = 0
    max_audio_gap_ms: float | None = None
    repeated_pts: int = 0
    pts_regressions: int = 0
    repeated_dts: int = 0
    dts_regressions: int = 0
    dropped_or_late_frames: int = 0
    frame_gap_samples_ms: list[float] = field(default_factory=list)
    max_frame_gap_ms: float | None = None
    termination_reason: TerminationReason = TerminationReason.APPLICATION_ERROR
    failure_category: str | None = None

    @property
    def observed_duration_seconds(self) -> float | None:
        if self.connection_started_at is None or self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.connection_started_at)

    @property
    def media_duration_seconds(self) -> float:
        if self.first_media_at is None or self.last_media_at is None:
            return 0.0
        return max(0.0, self.last_media_at - self.first_media_at)

    @property
    def time_to_first_media_seconds(self) -> float | None:
        if self.first_media_at is None:
            return None
        return max(0.0, self.first_media_at - self.requested_at)

    @property
    def time_to_first_decoded_frame_seconds(self) -> float | None:
        if self.first_decoded_frame_at is None:
            return None
        return max(0.0, self.first_decoded_frame_at - self.requested_at)

    @property
    def effective_fps(self) -> float | None:
        duration = self.media_duration_seconds
        if duration <= 0 or self.decoded_frames < 2:
            return None
        return (self.decoded_frames - 1) / duration

    @property
    def average_bitrate_bps(self) -> float | None:
        duration = self.media_duration_seconds
        if duration <= 0:
            return None
        return self.encoded_bytes * 8 / duration

    def frame_gap_percentile_ms(self, value: float) -> float | None:
        ordered = sorted(self.frame_gap_samples_ms)
        if not ordered:
            return None
        rank = (len(ordered) - 1) * value / 100
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


@dataclass(frozen=True, slots=True)
class ContinuitySummary:
    transition_gaps_ms: tuple[float, ...]
    minimum_gap_ms: float | None
    p50_gap_ms: float | None
    p95_gap_ms: float | None
    p99_gap_ms: float | None
    maximum_gap_ms: float | None
    total_blind_time_seconds: float
    media_availability_percent: float
    observation_window_seconds: float


@dataclass(frozen=True, slots=True)
class OverlapResult:
    attempted: bool
    supported_observed: str = "unknown"
    second_session_status: str | None = None
    first_frame_b_at: float | None = None
    last_frame_a_at: float | None = None
    overlap_duration_seconds: float | None = None
    disruption_to_a: bool | None = None
    resulting_gap_ms: float | None = None


@dataclass(frozen=True, slots=True)
class EngineeringTargets:
    critical_availability_percent: float = 99.9
    critical_p99_gap_ms: float = 500.0
    critical_max_blind_interval_ms: float = 1000.0
    operational_availability_percent: float = 99.5
    operational_p99_gap_ms: float = 2000.0


@dataclass(slots=True)
class CameraQualificationResult:
    camera_id: UUID
    label: str
    mode: QualificationMode
    session_class: SessionClass
    complete: bool
    sessions: list[SessionResult]
    continuity: ContinuitySummary
    decision: QualificationDecision
    overlap: OverlapResult = field(default_factory=lambda: OverlapResult(attempted=False))
    resource_samples: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def safe_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["camera_id"] = str(self.camera_id)
        value["mode"] = self.mode.value
        value["session_class"] = self.session_class.value
        value["decision"] = self.decision.value
        for session in value["sessions"]:
            session["termination_reason"] = session["termination_reason"].value
            session["configured_expected_limit_seconds"] = self.session_class.expected_limit_seconds
            session["observed_session_duration_seconds"] = self.sessions[
                session["session_number"] - 1
            ].observed_duration_seconds
            session["time_to_first_media_seconds"] = self.sessions[
                session["session_number"] - 1
            ].time_to_first_media_seconds
            session["time_to_first_decoded_frame_seconds"] = self.sessions[
                session["session_number"] - 1
            ].time_to_first_decoded_frame_seconds
            session["session_media_duration_seconds"] = self.sessions[
                session["session_number"] - 1
            ].media_duration_seconds
            session["effective_fps"] = self.sessions[session["session_number"] - 1].effective_fps
            session["average_bitrate_bps"] = self.sessions[
                session["session_number"] - 1
            ].average_bitrate_bps
            session["p50_frame_gap_ms"] = self.sessions[
                session["session_number"] - 1
            ].frame_gap_percentile_ms(50)
            session["p95_frame_gap_ms"] = self.sessions[
                session["session_number"] - 1
            ].frame_gap_percentile_ms(95)
            session["p99_frame_gap_ms"] = self.sessions[
                session["session_number"] - 1
            ].frame_gap_percentile_ms(99)
            session.pop("frame_gap_samples_ms", None)
        return value
