from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendExited,
    BackendFailed,
    DecodedCaps,
    EndOfStream,
    Heartbeat,
    MediaBatch,
    MediaNegotiated,
    TransportConnected,
)
from veotrex_edge_agent.camera_transport.descriptor import LiveSessionDescriptor, ProviderKind
from veotrex_edge_agent.camera_transport.errors import (
    TERMINAL_CATEGORIES,
    TransportErrorCategory,
)
from veotrex_edge_agent.camera_transport.health import (
    DecoderState,
    GapClass,
    ProviderState,
    StallPolicy,
    TransportHealth,
)
from veotrex_edge_agent.camera_transport.metrics import TransportMetrics
from veotrex_edge_agent.camera_transport.reconnect import ReconnectBudget, ReconnectPolicy
from veotrex_edge_agent.camera_transport.state import TransportState, TransportStateMachine
from veotrex_edge_agent.camera_transport.timing import MediaTimeline
from veotrex_edge_agent.qualification.metrics import BoundedSamples

_C = TransportErrorCategory
_CONNECT_CATEGORIES = frozenset(
    {
        _C.AUTHORIZATION_FAILED,
        _C.SESSION_ACQUIRE_TIMEOUT,
        _C.TRANSPORT_CONNECT_FAILED,
        _C.TRANSPORT_PROTOCOL_UNSUPPORTED,
        _C.REDIRECT_REFUSED,
        _C.INVALID_ENDPOINT,
        _C.PROVIDER_UNAVAILABLE,
        _C.PROVIDER_NOT_CONFIGURED,
        _C.CAMERA_OFFLINE,
        _C.FIRST_MEDIA_TIMEOUT,
    }
)
_DECODER_CATEGORIES = frozenset({_C.DECODER_START_FAILED, _C.DECODER_FAILED, _C.CODEC_UNSUPPORTED})
_PROVIDER_STATES = {
    _C.AUTHORIZATION_FAILED: ProviderState.AUTHORIZATION_FAILED,
    _C.PROVIDER_NOT_CONFIGURED: ProviderState.NOT_CONFIGURED,
    _C.CAMERA_OFFLINE: ProviderState.CAMERA_OFFLINE,
    _C.PROVIDER_UNAVAILABLE: ProviderState.UNAVAILABLE,
    _C.SESSION_ACQUIRE_TIMEOUT: ProviderState.UNAVAILABLE,
}
_ACTIVE = frozenset({TransportState.STREAMING, TransportState.DEGRADED, TransportState.RENEWING})


class AcquirePurpose(StrEnum):
    INITIAL = "INITIAL"
    RECONNECT = "RECONNECT"
    RENEWAL = "RENEWAL"


@dataclass(frozen=True, slots=True)
class AcquireSession:
    generation: int
    purpose: AcquirePurpose


@dataclass(frozen=True, slots=True)
class StartBackend:
    generation: int


@dataclass(frozen=True, slots=True)
class StopBackend:
    generation: int
    reason: str


Action = AcquireSession | StartBackend | StopBackend


@dataclass(frozen=True, slots=True)
class RenewalPolicy:
    replacement_first_media_timeout_seconds: float = 10.0
    max_attempts_per_session: int = 2
    retry_delay_seconds: float = 2.0
    expiry_end_of_stream_grace_seconds: float = 3.0

    def __post_init__(self) -> None:
        if not 0 < self.replacement_first_media_timeout_seconds <= 120:
            raise ValueError("replacement first-media timeout is out of bounds")
        if not 1 <= self.max_attempts_per_session <= 5:
            raise ValueError("renewal attempts must be between 1 and 5")
        if not 0 <= self.retry_delay_seconds <= 60:
            raise ValueError("renewal retry delay is out of bounds")
        if not 0 <= self.expiry_end_of_stream_grace_seconds <= 30:
            raise ValueError("expiry grace is out of bounds")


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    stall: StallPolicy = field(default_factory=StallPolicy)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    renewal: RenewalPolicy = field(default_factory=RenewalPolicy)
    acquire_timeout_seconds: float = 10.0
    require_decode: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.acquire_timeout_seconds <= 120:
            raise ValueError("acquire timeout is out of bounds")


@dataclass(slots=True)
class _Slot:
    generation: int
    purpose: AcquirePurpose
    requested_at: float
    descriptor: LiveSessionDescriptor | None = None
    acquired_at: float | None = None
    backend_started_at: float | None = None
    connected_at: float | None = None
    first_media_at: float | None = None
    last_media_at: float | None = None
    first_decoded_at: float | None = None
    last_decoded_at: float | None = None
    codec: str | None = None
    decoder: str | None = None
    hardware_decoder: bool | None = None
    width: int | None = None
    height: int | None = None
    framerate: float | None = None
    nvmm: bool | None = None
    compressed: int = 0
    decoded: int = 0


class TransportController:
    """Deterministic (sans-I/O) transport lifecycle for one logical camera.

    Inputs are provider results, backend events, and clock ticks; outputs are actions for the
    runner. Every decision uses the injected ``now`` (monotonic seconds), so the full failure
    matrix is testable without sleeps. At most two sessions exist: current and a renewal
    candidate.
    """

    def __init__(
        self,
        camera_id: UUID,
        provider: ProviderKind,
        config: ControllerConfig | None = None,
        *,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.camera_id = camera_id
        self.provider = provider
        self.config = config or ControllerConfig()
        self.machine = TransportStateMachine()
        self.budget = ReconnectBudget(self.config.reconnect, random_value)
        self.metrics = TransportMetrics(camera_id)
        self.timeline = MediaTimeline()
        self.handoff_gaps = BoundedSamples(1024)
        self.session_records: deque[dict[str, Any]] = deque(maxlen=1024)
        self.provider_state = ProviderState.IDLE
        self.last_failure: TransportErrorCategory | None = None
        self.last_renewal_failure: TransportErrorCategory | None = None
        self.circuit_open = False
        self.expiry_reconnects = 0
        self._next_generation = 1
        self._current: _Slot | None = None
        self._candidate: _Slot | None = None
        self._reconnect_at: float | None = None
        self._renewal_retry_at: float | None = None
        self._renewal_attempts = 0
        self._streaming_since: float | None = None
        self._max_generations = 1_000_000

    # ----------------------------------------------------------------- introspection
    @property
    def state(self) -> TransportState:
        return self.machine.state

    @property
    def current_generation(self) -> int | None:
        return self._current.generation if self._current else None

    @property
    def candidate_generation(self) -> int | None:
        return self._candidate.generation if self._candidate else None

    @property
    def active_generations(self) -> tuple[int, ...]:
        return tuple(slot.generation for slot in (self._current, self._candidate) if slot)

    @property
    def reconnect_at(self) -> float | None:
        return self._reconnect_at

    def descriptor_for(self, generation: int) -> LiveSessionDescriptor | None:
        slot = self._slot(generation)
        return slot.descriptor if slot else None

    # ----------------------------------------------------------------- helpers
    def _transition(self, state: TransportState, category: str, now: float) -> None:
        record = self.machine.transition(
            state, category=category, at=now, generation=self.current_generation
        )
        if record is None:
            return
        if state is TransportState.STREAMING:
            self._streaming_since = now if self._streaming_since is None else self._streaming_since
        elif state is not TransportState.RENEWING:
            self._streaming_since = None
        self._update_gauges(now)

    def _new_slot(self, purpose: AcquirePurpose, now: float) -> _Slot:
        if self._next_generation > self._max_generations:
            raise RuntimeError("session generation space exhausted")
        slot = _Slot(self._next_generation, purpose, now)
        self._next_generation += 1
        return slot

    def _slot(self, generation: int) -> _Slot | None:
        for slot in (self._current, self._candidate):
            if slot is not None and slot.generation == generation:
                return slot
        return None

    def _first_progress(self, slot: _Slot) -> float | None:
        return slot.first_decoded_at if self.config.require_decode else slot.first_media_at

    def _last_progress(self, slot: _Slot) -> float | None:
        return slot.last_decoded_at if self.config.require_decode else slot.last_media_at

    def _retire(self, slot: _Slot, reason: str, now: float) -> list[Action]:
        self.session_records.append(
            {
                "generation": slot.generation,
                "purpose": slot.purpose.value,
                "acquire_seconds": _delta(slot.acquired_at, slot.requested_at),
                "connect_seconds": _delta(slot.connected_at, slot.backend_started_at),
                "time_to_first_media_seconds": _delta(slot.first_media_at, slot.requested_at),
                "time_to_first_decoded_seconds": _delta(slot.first_decoded_at, slot.requested_at),
                "media_duration_seconds": _delta(slot.last_decoded_at, slot.first_decoded_at),
                "compressed_buffers": slot.compressed,
                "decoded_buffers": slot.decoded,
                "codec": slot.codec,
                "decoder": slot.decoder,
                "hardware_decoder": slot.hardware_decoder,
                "width": slot.width,
                "height": slot.height,
                "framerate": slot.framerate,
                "nvmm": slot.nvmm,
                "end_reason": reason,
                "ended_at": now,
            }
        )
        if slot.backend_started_at is not None:
            return [StopBackend(slot.generation, reason)]
        return []

    def _update_gauges(self, now: float) -> None:
        state = self.machine.state
        current = self._current
        progress = self._last_progress(current) if current else None
        flowing = (
            state in _ACTIVE
            and progress is not None
            and now - progress < self.config.stall.degraded_after_seconds
        )
        decoding = (
            current is not None
            and current.last_decoded_at is not None
            and now - current.last_decoded_at < self.config.stall.degraded_after_seconds
        )
        self.metrics.set_gauge("camera_transport_up", state in _ACTIVE)
        self.metrics.set_gauge("camera_media_flowing", flowing)
        self.metrics.set_gauge("camera_decoder_up", state in _ACTIVE and decoding)

    # ----------------------------------------------------------------- lifecycle inputs
    def start(self, now: float) -> list[Action]:
        if self.machine.state is not TransportState.STOPPED:
            raise RuntimeError("transport already started")
        self.circuit_open = False
        self._transition(TransportState.CONNECTING, "start", now)
        self._current = self._new_slot(AcquirePurpose.INITIAL, now)
        self.provider_state = ProviderState.ACQUIRING
        return [AcquireSession(self._current.generation, AcquirePurpose.INITIAL)]

    def stop(self, now: float, reason: str = "stop_requested") -> list[Action]:
        actions: list[Action] = []
        for slot in (self._candidate, self._current):
            if slot is not None:
                actions.extend(self._retire(slot, reason, now))
        self._current = self._candidate = None
        self._reconnect_at = None
        self._transition(TransportState.STOPPED, reason, now)
        self.provider_state = ProviderState.IDLE
        self._update_gauges(now)
        return actions

    def reset(self, now: float) -> list[Action]:
        """Explicit operator reset after FAILED/circuit-open; clears the reconnect budget."""
        actions = self.stop(now, "operator_reset")
        self.budget.reset()
        self.circuit_open = False
        self.last_failure = None
        return actions

    def on_session_acquired(
        self, generation: int, descriptor: LiveSessionDescriptor, now: float
    ) -> list[Action]:
        slot = self._slot(generation)
        if slot is None or slot.descriptor is not None:
            return []  # Abandoned acquisition: the runner discards the lease unused.
        if (
            descriptor.generation != generation
            or descriptor.logical_camera_id != self.camera_id
            or descriptor.provider is not self.provider
        ):
            return self._fail_slot(slot, _C.INTERNAL_TRANSPORT_ERROR, now)
        if descriptor.expired(now):
            return self._fail_slot(slot, _C.SESSION_EXPIRED, now)
        slot.descriptor = descriptor
        slot.acquired_at = now
        slot.backend_started_at = now
        self.metrics.inc("camera_transport_sessions_total")
        self.metrics.observe("camera_session_acquire_seconds", now - slot.requested_at)
        self.provider_state = ProviderState.SESSION_AUTHORIZED
        if slot is self._current:
            self.timeline.begin_instance(generation)
        return [StartBackend(generation)]

    def on_session_failed(
        self, generation: int, category: TransportErrorCategory, now: float
    ) -> list[Action]:
        slot = self._slot(generation)
        if slot is None:
            return []
        if slot is self._current:
            self.provider_state = _PROVIDER_STATES.get(category, self.provider_state)
        return self._fail_slot(slot, category, now)

    def on_event(self, event: BackendEvent, now: float) -> list[Action]:
        slot = self._slot(event.generation)
        if slot is None:
            if isinstance(event, MediaBatch):
                stale = len(event.compressed) + len(event.decoded)
                self.timeline.stale_rejected += stale
                self.metrics.inc("camera_transport_stale_generation_buffers_total", stale)
            return []
        if isinstance(event, TransportConnected):
            if slot.connected_at is None:
                slot.connected_at = event.at
            return []
        if isinstance(event, MediaNegotiated):
            slot.codec = event.codec.value
            slot.decoder = event.decoder
            slot.hardware_decoder = event.hardware_decoder
            return []
        if isinstance(event, DecodedCaps):
            slot.width, slot.height = event.width, event.height
            slot.framerate, slot.nvmm = event.framerate, event.nvmm
            return []
        if isinstance(event, Heartbeat):
            return []
        if isinstance(event, MediaBatch):
            return self._on_media(slot, event, now)
        if isinstance(event, BackendFailed):
            return self._fail_slot(slot, event.category, now)
        if isinstance(event, EndOfStream):
            descriptor = slot.descriptor
            grace = self.config.renewal.expiry_end_of_stream_grace_seconds
            expected = (
                descriptor is not None
                and descriptor.expires_monotonic is not None
                and event.at >= descriptor.expires_monotonic - grace
            )
            return self._fail_slot(slot, _C.SESSION_EXPIRED if expected else _C.END_OF_STREAM, now)
        if isinstance(event, BackendExited):
            return self._fail_slot(slot, _C.WORKER_EXITED, now)
        return []

    # ----------------------------------------------------------------- media
    def _on_media(self, slot: _Slot, batch: MediaBatch, now: float) -> list[Action]:
        had_progress = self._first_progress(slot) is not None
        for compressed in batch.compressed:
            slot.compressed += 1
            slot.first_media_at = slot.first_media_at or compressed.arrival
            slot.last_media_at = max(slot.last_media_at or compressed.arrival, compressed.arrival)
        for decoded in batch.decoded:
            slot.decoded += 1
            slot.first_decoded_at = slot.first_decoded_at or decoded.arrival
            slot.last_decoded_at = max(slot.last_decoded_at or decoded.arrival, decoded.arrival)
        if slot is self._candidate:
            if self._first_progress(slot) is None:
                return []
            handoff = self._handoff(now)
            self._record_media(slot, batch)
            return handoff
        previous_decoded = self.timeline.last_timestamp
        self._record_media(
            slot, batch, previous_decoded.arrival_monotonic if previous_decoded else None
        )
        actions: list[Action] = []
        if not had_progress and self._first_progress(slot) is not None:
            if slot.first_media_at is not None:
                self.metrics.observe(
                    "camera_time_to_first_media_seconds", slot.first_media_at - slot.requested_at
                )
            if slot.first_decoded_at is not None:
                self.metrics.observe(
                    "camera_time_to_first_decoded_buffer_seconds",
                    slot.first_decoded_at - slot.requested_at,
                )
            if self.machine.state is TransportState.CONNECTING:
                self._transition(TransportState.STREAMING, "first_media", now)
        elif self.machine.state is TransportState.DEGRADED and batch.decoded:
            self._transition(TransportState.STREAMING, "media_resumed", now)
        self._update_gauges(now)
        return actions

    def _record_media(
        self, slot: _Slot, batch: MediaBatch, previous_arrival: float | None = None
    ) -> None:
        if slot is not self._current:
            return
        for compressed in batch.compressed:
            self.timeline.accept_compressed(slot.generation, compressed)
        before = self.timeline.discontinuities
        for decoded in batch.decoded:
            stamp = self.timeline.accept_decoded(slot.generation, decoded)
            if stamp is None:
                continue
            if previous_arrival is not None and not stamp.discontinuity:
                self.metrics.observe(
                    "camera_inter_buffer_gap_seconds", max(0.0, decoded.arrival - previous_arrival)
                )
            previous_arrival = decoded.arrival
        self.metrics.inc("camera_transport_media_buffers_total", len(batch.compressed))
        self.metrics.inc("camera_transport_decoded_buffers_total", len(batch.decoded))
        added = self.timeline.discontinuities - before
        if added:
            self.metrics.inc("camera_transport_timestamp_discontinuities_total", added)

    def _handoff(self, now: float) -> list[Action]:
        old, new = self._current, self._candidate
        assert old is not None and new is not None
        first_new, last_old = self._first_progress(new), self._last_progress(old)
        if first_new is not None and last_old is not None:
            self.handoff_gaps.add(first_new - last_old)
        actions = self._retire(old, "renewal_handoff", now)
        self._current, self._candidate = new, None
        self._renewal_attempts = 0
        self._renewal_retry_at = None
        before = self.timeline.discontinuities
        self.timeline.begin_instance(new.generation)
        self.metrics.inc(
            "camera_transport_timestamp_discontinuities_total",
            self.timeline.discontinuities - before,
        )
        self.metrics.inc("camera_transport_renewals_total")
        self._transition(TransportState.STREAMING, "renewal_handoff", now)
        return actions

    # ----------------------------------------------------------------- failures
    def _fail_slot(self, slot: _Slot, category: TransportErrorCategory, now: float) -> list[Action]:
        if slot is self._candidate:
            return self._renewal_failed(category, now)
        return self._fail_current(category, now)

    def _renewal_failed(self, category: TransportErrorCategory, now: float) -> list[Action]:
        candidate = self._candidate
        assert candidate is not None
        self._candidate = None
        actions = self._retire(candidate, f"renewal_failed:{category.value}", now)
        self.last_renewal_failure = category
        self.metrics.inc("camera_transport_renewal_failures_total")
        self.metrics.failure(_C.RENEWAL_FAILED)
        self._renewal_retry_at = now + self.config.renewal.retry_delay_seconds
        if category in TERMINAL_CATEGORIES:
            self._renewal_attempts = self.config.renewal.max_attempts_per_session
        if self.machine.state is TransportState.RENEWING:
            current = self._current
            progress = self._last_progress(current) if current else None
            gap = now - progress if progress is not None else float("inf")
            degraded = self.config.stall.classify(gap) in {GapClass.DEGRADED, GapClass.STALLED}
            self._transition(
                TransportState.DEGRADED if degraded else TransportState.STREAMING,
                "renewal_failed",
                now,
            )
        return actions

    def _fail_current(self, category: TransportErrorCategory, now: float) -> list[Action]:
        current = self._current
        had_progress = current is not None and self._first_progress(current) is not None
        actions: list[Action] = []
        if self._candidate is not None:
            actions.extend(self._retire(self._candidate, "abandoned", now))
        if current is not None:
            actions.extend(self._retire(current, category.value, now))
        self._current = self._candidate = None
        self._renewal_attempts = 0
        self._renewal_retry_at = None
        expected_expiry = category is _C.SESSION_EXPIRED and had_progress
        if not expected_expiry:
            self.last_failure = category
            self.metrics.failure(category)
            if category in _CONNECT_CATEGORIES and not had_progress:
                self.metrics.inc("camera_transport_connect_failures_total")
            if category in _DECODER_CATEGORIES:
                self.metrics.inc("camera_transport_decoder_errors_total")
            if category is _C.MEDIA_STALLED and self.machine.state is not TransportState.DEGRADED:
                self.metrics.inc("camera_transport_stalls_total")
        if category in TERMINAL_CATEGORIES:
            self._transition(TransportState.FAILED, category.value, now)
            return actions
        if expected_expiry:
            # A session that delivered media and then reached its provider lifetime is not a
            # camera failure: reconnect at once without consuming the failure budget.
            delay: float | None = 0.0
            self.expiry_reconnects += 1
        else:
            delay = self.budget.next_delay(now)
        if delay is None:
            self.circuit_open = True
            self._transition(TransportState.FAILED, "circuit_open", now)
            return actions
        self._reconnect_at = now + delay
        self.metrics.inc("camera_transport_reconnects_total")
        self._transition(TransportState.RECONNECTING, category.value, now)
        return actions

    def _on_current_expired(self, now: float) -> list[Action]:
        current, candidate = self._current, self._candidate
        assert current is not None
        if candidate is None or candidate.descriptor is None:
            return self._fail_current(_C.SESSION_EXPIRED, now)
        # Replacement exists but has not produced media: accept a blackout rather than keep an
        # expired session. Counted as a renewal failure because handoff was not seamless.
        actions = self._retire(current, "session_expired", now)
        self._current, self._candidate = candidate, None
        self._renewal_attempts = 0
        self.metrics.inc("camera_transport_renewal_failures_total")
        self.metrics.failure(_C.RENEWAL_FAILED)
        before = self.timeline.discontinuities
        self.timeline.begin_instance(candidate.generation)
        self.metrics.inc(
            "camera_transport_timestamp_discontinuities_total",
            self.timeline.discontinuities - before,
        )
        self._transition(TransportState.CONNECTING, "expired_before_handoff", now)
        return actions

    # ----------------------------------------------------------------- clock
    def tick(self, now: float) -> list[Action]:
        state = self.machine.state
        if state is TransportState.RECONNECTING:
            if self._reconnect_at is not None and now >= self._reconnect_at:
                self._reconnect_at = None
                self._transition(TransportState.CONNECTING, "reconnect_attempt", now)
                self._current = self._new_slot(AcquirePurpose.RECONNECT, now)
                self.provider_state = ProviderState.ACQUIRING
                return [AcquireSession(self._current.generation, AcquirePurpose.RECONNECT)]
            return []
        current = self._current
        if state in {TransportState.STOPPED, TransportState.FAILED} or current is None:
            return []
        stall = self.config.stall
        if current.descriptor is None:
            if now - current.requested_at >= self.config.acquire_timeout_seconds:
                self.provider_state = ProviderState.UNAVAILABLE
                return self._fail_current(_C.SESSION_ACQUIRE_TIMEOUT, now)
            return []
        if current.descriptor.expired(now):
            return self._on_current_expired(now)
        if state is TransportState.CONNECTING:
            started = current.backend_started_at
            if (
                started is not None
                and self._first_progress(current) is None
                and now - started >= stall.first_media_timeout_seconds
            ):
                if current.connected_at is None:
                    category = _C.TRANSPORT_CONNECT_FAILED
                elif current.compressed and self.config.require_decode:
                    category = _C.DECODER_START_FAILED
                else:
                    category = _C.FIRST_MEDIA_TIMEOUT
                return self._fail_current(category, now)
            return []
        actions: list[Action] = []
        progress = self._last_progress(current)
        assert progress is not None
        gap = now - progress
        media_recent = (
            current.last_media_at is not None
            and now - current.last_media_at < stall.degraded_after_seconds
        )
        if (
            self.config.require_decode
            and media_recent
            and gap >= stall.decoder_progress_timeout_seconds
        ):
            return self._fail_current(_C.DECODER_FAILED, now)
        gap_class = stall.classify(gap)
        if gap_class is GapClass.STALLED:
            return self._fail_current(_C.MEDIA_STALLED, now)
        if gap_class is GapClass.DEGRADED and state is TransportState.STREAMING:
            self.metrics.inc("camera_transport_stalls_total")
            self._transition(TransportState.DEGRADED, "media_gap", now)
            state = TransportState.DEGRADED
        if (
            self._streaming_since is not None
            and now - self._streaming_since >= self.config.reconnect.stable_reset_seconds
            and (self.budget.attempts_in_window or self.budget.consecutive_failures)
        ):
            self.budget.reset()
        actions.extend(self._renewal_tick(current, state, now))
        self._update_gauges(now)
        return actions

    def _renewal_tick(self, current: _Slot, state: TransportState, now: float) -> list[Action]:
        descriptor = current.descriptor
        assert descriptor is not None
        candidate = self._candidate
        policy = self.config.renewal
        if candidate is None:
            if (
                descriptor.renew_after_monotonic is not None
                and now >= descriptor.renew_after_monotonic
                and self._renewal_attempts < policy.max_attempts_per_session
                and (self._renewal_retry_at is None or now >= self._renewal_retry_at)
                and state in {TransportState.STREAMING, TransportState.DEGRADED}
            ):
                self._renewal_attempts += 1
                self._candidate = self._new_slot(AcquirePurpose.RENEWAL, now)
                self._transition(TransportState.RENEWING, "renewal_due", now)
                return [AcquireSession(self._candidate.generation, AcquirePurpose.RENEWAL)]
            return []
        if candidate.descriptor is None:
            if now - candidate.requested_at >= self.config.acquire_timeout_seconds:
                return self._renewal_failed(_C.SESSION_ACQUIRE_TIMEOUT, now)
            return []
        started = candidate.backend_started_at
        if (
            started is not None
            and self._first_progress(candidate) is None
            and now - started >= policy.replacement_first_media_timeout_seconds
        ):
            return self._renewal_failed(_C.FIRST_MEDIA_TIMEOUT, now)
        return []

    # ----------------------------------------------------------------- health
    def health(self, now: float) -> TransportHealth:
        state = self.machine.state
        current = self._current
        descriptor = current.descriptor if current else None
        progress = self._last_progress(current) if current else None
        gap = now - progress if progress is not None else None
        degraded = self.config.stall.degraded_after_seconds
        media_flowing = state in _ACTIVE and gap is not None and gap < degraded
        decoder_recent = (
            current is not None
            and current.last_decoded_at is not None
            and now - current.last_decoded_at < degraded
        )
        if current is None or current.backend_started_at is None:
            decoder_state = (
                DecoderState.FAILED
                if state is TransportState.FAILED and (self.last_failure in _DECODER_CATEGORIES)
                else DecoderState.NOT_STARTED
            )
        elif current.first_decoded_at is None:
            decoder_state = DecoderState.NEGOTIATING
        elif decoder_recent:
            decoder_state = DecoderState.DECODING
        else:
            decoder_state = DecoderState.STALLED
        stamp = self.timeline.last_timestamp
        return TransportHealth(
            logical_camera_id=str(self.camera_id),
            provider=self.provider.value,
            provider_state=self.provider_state.value,
            camera_available=(
                False
                if self.provider_state is ProviderState.CAMERA_OFFLINE
                else (True if media_flowing else None)
            ),
            session_authorized=descriptor is not None,
            transport_connected=current is not None and current.connected_at is not None,
            media_flowing=media_flowing,
            decoder_healthy=state in _ACTIVE and decoder_recent,
            transport_state=state.value,
            decoder_state=decoder_state.value,
            transport_protocol=descriptor.transport_protocol.value if descriptor else None,
            codec=current.codec if current else None,
            decoder=current.decoder if current else None,
            hardware_decode=current.hardware_decoder if current else None,
            session_generation=current.generation if current else None,
            last_media_pts_ns=stamp.pts_ns if stamp else None,
            last_media_monotonic=stamp.arrival_monotonic if stamp else None,
            recent_buffer_rate_fps=self.timeline.recent_rate(now),
            recent_gap_seconds=gap,
            reconnect_count=self.metrics.counters["camera_transport_reconnects_total"],
            renewal_count=self.metrics.counters["camera_transport_renewals_total"],
            stall_count=self.metrics.counters["camera_transport_stalls_total"],
            session_expiry_remaining_seconds=descriptor.expiry_remaining(now)
            if descriptor
            else None,
            last_failure_category=self.last_failure.value if self.last_failure else None,
            circuit_open=self.circuit_open,
        )


def _delta(end: float | None, start: float | None) -> float | None:
    if end is None or start is None:
        return None
    return end - start
