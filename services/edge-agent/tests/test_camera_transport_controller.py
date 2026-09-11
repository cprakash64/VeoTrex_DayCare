import asyncio
import threading
import time
from collections.abc import Callable
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendExited,
    BackendFailed,
    EndOfStream,
    MediaBatch,
    TransportConnected,
)
from veotrex_edge_agent.camera_transport.controller import (
    AcquirePurpose,
    AcquireSession,
    ControllerConfig,
    RenewalPolicy,
    StartBackend,
    StopBackend,
    TransportController,
)
from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.health import StallPolicy
from veotrex_edge_agent.camera_transport.metrics import label_sets, render_prometheus
from veotrex_edge_agent.camera_transport.provider import (
    FAKE_ENDPOINT,
    FAKE_SECRET,
    FakeLiveSessionProvider,
    FakeOutcome,
)
from veotrex_edge_agent.camera_transport.reconnect import ReconnectBudget, ReconnectPolicy
from veotrex_edge_agent.camera_transport.runner import CameraTransportRunner
from veotrex_edge_agent.camera_transport.state import (
    InvalidTransportTransition,
    TransportState,
    TransportStateMachine,
)
from veotrex_edge_agent.camera_transport.timing import (
    CompressedSample,
    DecodedSample,
    MediaTimeline,
    TimestampSource,
)

CAMERA = UUID(int=0x5A)
DT = 1 / 15
S = TransportState


def descriptor(
    generation: int, now: float, lifetime: float | None = None, lead: float | None = None
) -> LiveSessionDescriptor:
    expires = now + lifetime if lifetime else None
    renew = expires - lead if expires is not None and lead else None
    return LiveSessionDescriptor(
        provider=ProviderKind.FAKE,
        logical_camera_id=CAMERA,
        generation=generation,
        endpoint=validate_endpoint(FAKE_ENDPOINT, LOCAL_FIXTURE_ENDPOINT_POLICY),
        created_monotonic=now,
        expires_monotonic=expires,
        renew_after_monotonic=renew,
    )


def media(
    generation: int,
    start: float,
    count: int = 3,
    *,
    pts0: int = 0,
    decoded: bool = True,
    step: float = DT,
) -> MediaBatch:
    arrivals = [start + index * step for index in range(count)]
    pts = [pts0 + int(index * step * 1e9) for index in range(count)]
    return MediaBatch(
        generation,
        tuple(CompressedSample(a, p, p, 1200) for a, p in zip(arrivals, pts, strict=True)),
        tuple(DecodedSample(a, p) for a, p in zip(arrivals, pts, strict=True)) if decoded else (),
    )


def make(**config: object) -> TransportController:
    return TransportController(
        CAMERA, ProviderKind.FAKE, ControllerConfig(**config), random_value=lambda: 0.5
    )


def connect(
    controller: TransportController,
    now: float = 0.0,
    lifetime: float | None = None,
    lead: float | None = None,
) -> int:
    if controller.state is S.STOPPED:
        generation = controller.start(now)[0].generation  # type: ignore[union-attr]
    else:
        generation = controller.current_generation  # type: ignore[assignment]
    actions = controller.on_session_acquired(
        generation, descriptor(generation, now, lifetime, lead), now
    )
    assert actions == [StartBackend(generation)]
    controller.on_event(TransportConnected(generation, now + 0.1), now + 0.1)
    controller.on_event(media(generation, now + 0.2), now + 0.4)
    assert controller.state is S.STREAMING
    return generation


def stream_until(
    controller: TransportController, generation: int, start: float, end: float
) -> None:
    """Feed continuous media and ticks; nothing (media or tick) happens after ``end``."""
    t = start
    while t + 3 * DT <= end + 1e-9:
        controller.on_event(media(generation, t, 3, pts0=int(t * 1e9)), t + 3 * DT)
        controller.tick(t + 3 * DT)
        t += 3 * DT


# --------------------------------------------------------------------- state machine basics
def test_state_machine_rejects_illegal_transitions() -> None:
    machine = TransportStateMachine()
    with pytest.raises(InvalidTransportTransition):
        machine.transition(S.STREAMING, category="x", at=0.0)
    machine.transition(S.CONNECTING, category="start", at=0.0)
    machine.transition(S.FAILED, category="auth", at=1.0)
    with pytest.raises(InvalidTransportTransition):
        machine.transition(S.CONNECTING, category="retry", at=2.0)
    assert machine.transition(S.FAILED, category="again", at=3.0) is None


def test_connection_transition_requires_media_not_process_or_socket() -> None:
    controller = make()
    [acquire] = controller.start(0.0)
    assert isinstance(acquire, AcquireSession) and acquire.purpose is AcquirePurpose.INITIAL
    assert controller.state is S.CONNECTING
    generation = acquire.generation
    assert controller.on_session_acquired(generation, descriptor(generation, 0.0), 0.1) == [
        StartBackend(generation)
    ]
    controller.on_event(TransportConnected(generation, 0.2), 0.2)
    health = controller.health(0.3)
    assert controller.state is S.CONNECTING
    assert health.session_authorized and health.transport_connected
    assert not health.media_flowing and not health.decoder_healthy
    assert health.decoder_state == "NEGOTIATING"
    # Compressed media alone is not STREAMING when decode is required.
    controller.on_event(media(generation, 0.4, decoded=False), 0.5)
    assert controller.state is S.CONNECTING


def test_first_decoded_media_enters_streaming_and_records_latency() -> None:
    controller = make()
    connect(controller)
    health = controller.health(0.5)
    assert health.media_flowing and health.decoder_healthy
    assert health.transport_state == "STREAMING"
    histograms = controller.metrics.histograms
    assert histograms["camera_time_to_first_decoded_buffer_seconds"].count == 1
    assert histograms["camera_session_acquire_seconds"].count == 1
    assert controller.metrics.gauges["camera_transport_up"] == 1


def test_connected_without_media_times_out_as_first_media_timeout() -> None:
    controller = make(stall=StallPolicy(first_media_timeout_seconds=5.0))
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(generation, descriptor(generation, 0.0), 0.0)
    controller.on_event(TransportConnected(generation, 0.5), 0.5)
    assert controller.tick(4.9) == []
    actions = controller.tick(5.0)
    assert StopBackend(generation, "FIRST_MEDIA_TIMEOUT") in actions
    assert controller.state is S.RECONNECTING
    assert controller.last_failure is C.FIRST_MEDIA_TIMEOUT


def test_never_connected_is_a_transport_connect_failure() -> None:
    controller = make(stall=StallPolicy(first_media_timeout_seconds=5.0))
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(generation, descriptor(generation, 0.0), 0.0)
    controller.tick(5.0)
    assert controller.last_failure is C.TRANSPORT_CONNECT_FAILED
    assert controller.metrics.counters["camera_transport_connect_failures_total"] == 1


# --------------------------------------------------------------------- stalls
def test_stall_transition_and_recovery_from_short_stall() -> None:
    controller = make()
    generation = connect(controller)
    last = 0.2 + 2 * DT
    assert controller.tick(last + 1.5) == []  # jitter: still STREAMING
    assert controller.state is S.STREAMING
    controller.tick(last + 2.1)
    assert controller.state is S.DEGRADED
    assert controller.metrics.counters["camera_transport_stalls_total"] == 1
    assert not controller.health(last + 2.1).media_flowing
    controller.on_event(media(generation, last + 3.0, pts0=3_000_000_000), last + 3.2)
    assert controller.state is S.STREAMING
    assert controller.metrics.counters["camera_transport_reconnects_total"] == 0


def test_prolonged_stall_fails_stream_and_schedules_bounded_reconnect() -> None:
    controller = make()
    generation = connect(controller)
    last = 0.2 + 2 * DT
    controller.tick(last + 2.5)
    actions = controller.tick(last + 8.0)
    assert actions == [StopBackend(generation, "MEDIA_STALLED")]
    assert controller.state is S.RECONNECTING
    assert controller.last_failure is C.MEDIA_STALLED
    assert controller.reconnect_at == pytest.approx(last + 8.0 + 1.0)


def test_decoder_failure_is_distinguished_from_transport_stall() -> None:
    controller = make()
    generation = connect(controller)
    t = 0.2 + 3 * DT
    while t < 6.0:
        controller.on_event(media(generation, t, decoded=False), t + 0.2)
        controller.tick(t + 0.2)
        t += 0.2
    assert controller.last_failure is C.DECODER_FAILED
    assert controller.metrics.counters["camera_transport_decoder_errors_total"] == 1
    assert controller.state is S.RECONNECTING


def test_decoder_start_failure_when_compressed_media_never_decodes() -> None:
    controller = make(stall=StallPolicy(first_media_timeout_seconds=5.0))
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(generation, descriptor(generation, 0.0), 0.0)
    controller.on_event(TransportConnected(generation, 0.1), 0.1)
    controller.on_event(media(generation, 0.2, decoded=False), 0.3)
    controller.tick(5.0)
    assert controller.last_failure is C.DECODER_START_FAILED


def test_backend_decoder_failure_event() -> None:
    controller = make()
    generation = connect(controller)
    controller.on_event(BackendFailed(generation, 1.0, C.DECODER_FAILED), 1.0)
    assert controller.state is S.RECONNECTING
    assert controller.metrics.counters["camera_transport_decoder_errors_total"] == 1


# --------------------------------------------------------------------- reconnect policy
def test_reconnect_backoff_is_exponential_bounded_and_jittered() -> None:
    policy = ReconnectPolicy(
        initial_delay_seconds=1, maximum_delay_seconds=8, jitter_ratio=0.2, max_attempts=10
    )
    budget = ReconnectBudget(policy, random_value=lambda: 0.5)
    assert [budget.next_delay(float(i)) for i in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    high = ReconnectBudget(policy, random_value=lambda: 1.0)
    low = ReconnectBudget(policy, random_value=lambda: 0.0)
    assert high.next_delay(0.0) == pytest.approx(1.2)
    assert low.next_delay(0.0) == pytest.approx(0.8)
    with pytest.raises(ValueError):
        ReconnectPolicy(initial_delay_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(max_attempts=0)


def test_reconnect_circuit_opens_after_bounded_attempts() -> None:
    controller = make(reconnect=ReconnectPolicy(max_attempts=3, window_seconds=600))
    now = 0.0
    generation = controller.start(now)[0].generation  # type: ignore[union-attr]
    for _ in range(3):
        controller.on_session_failed(generation, C.PROVIDER_UNAVAILABLE, now)
        assert controller.state is S.RECONNECTING
        now = controller.reconnect_at  # type: ignore[assignment]
        [acquire] = controller.tick(now)
        generation = acquire.generation  # type: ignore[union-attr]
    controller.on_session_failed(generation, C.PROVIDER_UNAVAILABLE, now)
    assert controller.state is S.FAILED
    assert controller.circuit_open
    assert controller.tick(now + 10_000) == []
    assert controller.metrics.counters["camera_transport_reconnects_total"] == 3
    assert controller.reset(now + 1) == []
    assert controller.state is S.STOPPED and not controller.circuit_open


def test_reconnect_budget_resets_after_stable_streaming() -> None:
    controller = make(reconnect=ReconnectPolicy(max_attempts=3, stable_reset_seconds=10))
    generation = connect(controller)
    controller.on_event(BackendExited(generation, 1.0), 1.0)
    assert controller.budget.attempts_in_window == 1
    [acquire] = controller.tick(controller.reconnect_at)  # type: ignore[arg-type]
    generation = connect(controller, now=3.0)
    assert acquire.generation == generation
    stream_until(controller, generation, 3.5, 14.0)
    assert controller.budget.attempts_in_window == 0
    assert controller.budget.consecutive_failures == 0


def test_camera_offline_is_retryable_and_reported_as_unavailable_camera() -> None:
    controller = make()
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.on_session_failed(generation, C.CAMERA_OFFLINE, 0.1)
    health = controller.health(0.2)
    assert controller.state is S.RECONNECTING
    assert health.provider_state == "CAMERA_OFFLINE"
    assert health.camera_available is False
    assert not health.session_authorized


def test_authorization_failure_is_terminal_without_retry_loop() -> None:
    controller = make()
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.on_session_failed(generation, C.AUTHORIZATION_FAILED, 0.1)
    assert controller.state is S.FAILED
    assert controller.health(0.2).provider_state == "AUTHORIZATION_FAILED"
    assert controller.tick(1000.0) == []
    assert controller.metrics.counters["camera_transport_reconnects_total"] == 0
    other = make()
    generation = connect(other)
    other.on_event(BackendFailed(generation, 1.0, C.AUTHORIZATION_FAILED), 1.0)
    assert other.state is S.FAILED


def test_provider_timeout_via_controller_deadline_and_runner_report() -> None:
    controller = make(acquire_timeout_seconds=4.0)
    controller.start(0.0)
    assert controller.tick(3.9) == []
    controller.tick(4.0)
    assert controller.last_failure is C.SESSION_ACQUIRE_TIMEOUT
    assert controller.health(4.0).provider_state == "UNAVAILABLE"
    other = make()
    generation = other.start(0.0)[0].generation  # type: ignore[union-attr]
    other.on_session_failed(generation, C.SESSION_ACQUIRE_TIMEOUT, 1.0)
    assert other.state is S.RECONNECTING


def test_late_acquisition_after_abandonment_is_ignored() -> None:
    controller = make(acquire_timeout_seconds=2.0)
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    controller.tick(2.0)
    assert controller.on_session_acquired(generation, descriptor(generation, 2.5), 2.5) == []


def test_unexpected_end_of_stream_and_worker_exit_reconnect() -> None:
    controller = make()
    generation = connect(controller)
    controller.on_event(EndOfStream(generation, 1.0), 1.0)
    assert controller.last_failure is C.END_OF_STREAM
    controller.tick(controller.reconnect_at)  # type: ignore[arg-type]
    generation = connect(controller, now=3.0)
    controller.on_event(BackendExited(generation, 4.0), 4.0)
    assert controller.last_failure is C.WORKER_EXITED


# --------------------------------------------------------------------- renewal
def test_successful_overlap_renewal_hands_off_without_blackout() -> None:
    controller = make()
    old = connect(controller, lifetime=30.0, lead=5.0)
    stream_until(controller, old, 0.5, 25.0)
    actions = controller.tick(25.05)
    assert controller.state is S.RENEWING
    [acquire] = actions
    assert isinstance(acquire, AcquireSession) and acquire.purpose is AcquirePurpose.RENEWAL
    new = acquire.generation
    assert controller.on_session_acquired(new, descriptor(new, 25.1, 30.0, 5.0), 25.1) == [
        StartBackend(new)
    ]
    controller.on_event(media(old, 25.1, 6, pts0=25_100_000_000), 25.5)  # old keeps flowing
    assert controller.current_generation == old
    before = controller.timeline.decoded_buffers
    actions = controller.on_event(media(new, 25.4, 3, pts0=0), 25.6)  # overlap: new starts early
    assert actions == [StopBackend(old, "renewal_handoff")]
    assert controller.state is S.STREAMING
    assert controller.current_generation == new
    assert controller.metrics.counters["camera_transport_renewals_total"] == 1
    assert controller.handoff_gaps.maximum is not None and controller.handoff_gaps.maximum < 0
    assert controller.timeline.decoded_buffers == before + 3
    assert controller.timeline.last_timestamp.stream_instance == new  # type: ignore[union-attr]


def test_stale_generation_media_is_rejected_after_handoff() -> None:
    controller = make()
    old = connect(controller, lifetime=30.0, lead=5.0)
    stream_until(controller, old, 0.5, 25.0)
    new = controller.tick(25.05)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(new, descriptor(new, 25.1, 30.0, 5.0), 25.1)
    controller.on_event(media(new, 25.4), 25.5)
    decoded = controller.timeline.decoded_buffers
    assert controller.on_event(media(old, 25.6, 4, pts0=99_000_000_000), 25.7) == []
    assert controller.timeline.decoded_buffers == decoded
    assert controller.timeline.stale_rejected == 8
    assert controller.metrics.counters["camera_transport_stale_generation_buffers_total"] == 8
    assert controller.timeline.last_timestamp.stream_instance == new  # type: ignore[union-attr]


def test_failed_renewal_keeps_old_session_and_retries_bounded() -> None:
    controller = make(renewal=RenewalPolicy(max_attempts_per_session=2, retry_delay_seconds=1.0))
    old = connect(controller, lifetime=30.0, lead=6.0)
    stream_until(controller, old, 0.5, 24.0)
    first = controller.tick(24.05)[0].generation  # type: ignore[union-attr]
    controller.on_session_failed(first, C.PROVIDER_UNAVAILABLE, 24.1)
    assert controller.state is S.STREAMING
    assert controller.current_generation == old
    assert controller.metrics.counters["camera_transport_renewal_failures_total"] == 1
    assert controller.tick(24.5) == []  # retry delay
    stream_until(controller, old, 24.2, 25.2)
    second = controller.candidate_generation
    assert second is not None and second > first
    assert controller.state is S.RENEWING


def test_replacement_session_without_media_fails_renewal_then_old_expires() -> None:
    controller = make(
        renewal=RenewalPolicy(
            max_attempts_per_session=1, replacement_first_media_timeout_seconds=3.0
        )
    )
    old = connect(controller, lifetime=30.0, lead=5.0)
    stream_until(controller, old, 0.5, 25.0)
    new = controller.tick(25.05)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(new, descriptor(new, 25.1, 30.0, 5.0), 25.1)
    controller.on_event(TransportConnected(new, 25.3), 25.3)
    stream_until(controller, old, 25.1, 28.4)
    assert controller.candidate_generation is None
    assert controller.last_renewal_failure is C.FIRST_MEDIA_TIMEOUT
    assert controller.metrics.counters["camera_transport_renewal_failures_total"] == 1
    stream_until(controller, old, 28.2, 29.9)
    attempts = controller.budget.attempts_in_window
    actions = controller.tick(30.0)
    assert StopBackend(old, "SESSION_EXPIRED") in actions
    assert controller.state is S.RECONNECTING
    assert controller.reconnect_at == 30.0  # expected expiry: immediate
    assert controller.budget.attempts_in_window == attempts  # did not consume failure budget
    assert controller.expiry_reconnects == 1


def test_old_session_expiry_promotes_acquired_replacement_with_explicit_discontinuity() -> None:
    controller = make()
    old = connect(controller, lifetime=30.0, lead=2.0)
    stream_until(controller, old, 0.5, 28.0)
    new = controller.tick(28.05)[0].generation  # type: ignore[union-attr]
    controller.on_session_acquired(new, descriptor(new, 28.1, 30.0, 2.0), 28.1)
    stream_until(controller, old, 28.1, 29.95)
    actions = controller.tick(30.0)
    assert StopBackend(old, "session_expired") in actions
    assert controller.state is S.CONNECTING
    assert controller.current_generation == new
    controller.on_event(media(new, 30.5), 30.6)
    assert controller.state is S.STREAMING
    assert controller.timeline.last_timestamp.discontinuity is False  # type: ignore[union-attr]
    assert controller.timeline.discontinuities == 1


def test_repeated_renewals_increase_generation_and_mark_each_instance() -> None:
    controller = make()
    generation = connect(controller, lifetime=20.0, lead=4.0)
    t = 0.5
    for _ in range(5):
        renew_at = controller.descriptor_for(generation).renew_after_monotonic  # type: ignore[union-attr]
        assert renew_at is not None
        stream_until(controller, generation, t, renew_at - 0.01)
        new = controller.tick(renew_at + 0.01)[0].generation  # type: ignore[union-attr]
        start = renew_at + 0.05
        controller.on_session_acquired(new, descriptor(new, start, 20.0, 4.0), start)
        controller.on_event(media(new, start + 0.3), start + 0.4)
        assert controller.current_generation == new > generation
        generation, t = new, start + 0.5
    assert controller.metrics.counters["camera_transport_renewals_total"] == 5
    assert controller.timeline.instances_started == 6
    assert controller.timeline.discontinuities == 5
    assert controller.metrics.counters["camera_transport_timestamp_discontinuities_total"] == 5


# --------------------------------------------------------------------- timestamps
def test_timestamp_progression_regression_missing_and_discontinuity() -> None:
    timeline = MediaTimeline(max_reversal_ns=500_000_000)
    timeline.begin_instance(1)
    first = timeline.accept_decoded(1, DecodedSample(1.0, 0))
    assert first is not None and first.discontinuity and first.source is TimestampSource.MEDIA_PTS
    for index in range(1, 5):
        stamp = timeline.accept_decoded(1, DecodedSample(1.0 + index * DT, int(index * DT * 1e9)))
        assert stamp is not None and not stamp.discontinuity
    assert timeline.summary()["decoded_pts_regressions"] == 0
    small = timeline.accept_decoded(1, DecodedSample(1.5, int(3 * DT * 1e9)))
    assert small is not None and not small.discontinuity
    assert timeline.summary()["decoded_pts_regressions"] == 1
    large = timeline.accept_decoded(1, DecodedSample(1.6, 0))
    timeline.accept_decoded(1, DecodedSample(1.7, 5_000_000_000))
    reversal = timeline.accept_decoded(1, DecodedSample(1.8, 1_000_000_000))
    assert reversal is not None and reversal.discontinuity
    assert large is not None
    assert timeline.large_reversals == 1
    missing = timeline.accept_decoded(1, DecodedSample(1.9, None))
    assert missing is not None and missing.source is TimestampSource.MISSING
    duplicate = timeline.accept_decoded(1, DecodedSample(2.0, 1_000_000_000))
    assert duplicate is not None
    assert timeline.summary()["decoded_pts_duplicates"] == 1
    timeline.begin_instance(2)
    fresh = timeline.accept_decoded(2, DecodedSample(2.1, 0))
    assert fresh is not None and fresh.discontinuity and fresh.sequence == 1
    assert timeline.accept_decoded(1, DecodedSample(2.2, 10)) is None
    with pytest.raises(ValueError):
        timeline.begin_instance(2)


def test_timeline_gap_statistics_are_bounded() -> None:
    timeline = MediaTimeline(gap_sample_capacity=16)
    timeline.begin_instance(1)
    for index in range(100):
        timeline.accept_decoded(1, DecodedSample(index * 0.1, index))
    summary = timeline.summary()
    assert summary["inter_buffer_gap_samples"] == 99
    assert summary["inter_buffer_gap_retained"] == 16
    assert summary["p50_gap_ms"] == pytest.approx(100.0)


# --------------------------------------------------------------------- lifecycle
def test_normal_shutdown_stops_backends_and_clears_state() -> None:
    controller = make()
    generation = connect(controller)
    actions = controller.stop(1.0)
    assert actions == [StopBackend(generation, "stop_requested")]
    assert controller.state is S.STOPPED
    assert controller.active_generations == ()
    assert controller.metrics.gauges == {
        "camera_transport_up": 0,
        "camera_media_flowing": 0,
        "camera_decoder_up": 0,
    }


def test_shutdown_during_connecting_releases_without_backend() -> None:
    controller = make()
    generation = controller.start(0.0)[0].generation  # type: ignore[union-attr]
    assert controller.stop(0.1) == []
    assert controller.on_session_acquired(generation, descriptor(generation, 0.2), 0.2) == []
    assert controller.on_event(media(generation, 0.3), 0.3) == []
    controller2 = make()
    generation = controller2.start(0.0)[0].generation  # type: ignore[union-attr]
    controller2.on_session_acquired(generation, descriptor(generation, 0.0), 0.0)
    assert controller2.stop(0.1) == [StopBackend(generation, "stop_requested")]


def test_repeated_start_stop_is_bounded_and_monotonic() -> None:
    controller = make()
    seen: list[int] = []
    for cycle in range(25):
        generation = connect(controller, now=cycle * 10.0)
        seen.append(generation)
        controller.stop(cycle * 10.0 + 1)
        assert controller.active_generations == ()
    assert seen == sorted(set(seen))
    assert len(controller.session_records) == 25
    with pytest.raises(RuntimeError):
        controller.start(1000.0)
        controller.start(1000.0)


# --------------------------------------------------------------------- metrics
def test_metrics_cardinality_is_bounded_to_camera_and_taxonomy() -> None:
    controllers = [TransportController(UUID(int=index), ProviderKind.FAKE) for index in (1, 2, 3)]
    for controller in controllers:
        for category in C:
            controller.metrics.failure(category)
    labels = label_sets(c.metrics for c in controllers)
    assert len(labels) == 3 * (1 + len(C))
    text = render_prometheus(c.metrics for c in controllers)
    for line in text.splitlines():
        label_block = line.split("{", 1)[1].split("}", 1)[0]
        for pair in label_block.split(","):
            key, _, _ = pair.partition("=")
            assert key in {"camera_id", "category", "le"}
    assert "127.0.0.1" not in text and "synthetic" not in text
    with pytest.raises(TypeError):
        make().metrics.__class__("camera-with-token")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        make().metrics.inc("camera_transport_token_total")


# --------------------------------------------------------------------- runner + logging
class FakeBackend:
    """Emits synthetic media from a thread, like the real worker's reader."""

    def __init__(self, generation: int, *, fail_after: float | None = None) -> None:
        self.generation = generation
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = False
        self.stopped = False
        self._fail_after = fail_after
        self.lease_repr = ""

    @property
    def pid(self) -> int | None:
        return None

    def start(self, lease: LiveSessionLease, emit: Callable[[BackendEvent], None]) -> None:
        self.started = True
        self.lease_repr = repr(lease)

        def run() -> None:
            start = time.monotonic()
            emit(TransportConnected(self.generation, start))
            pts = 0
            while not self._stop.wait(0.02):
                now = time.monotonic()
                if self._fail_after is not None and now - start > self._fail_after:
                    emit(BackendFailed(self.generation, now, C.TRANSPORT_DISCONNECTED))
                    return
                emit(
                    MediaBatch(
                        self.generation,
                        (CompressedSample(now, pts, pts, 900),),
                        (DecodedSample(now, pts),),
                    )
                )
                pts += 20_000_000

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


async def test_runner_streams_reconnects_and_never_logs_secrets() -> None:
    provider = FakeLiveSessionProvider()
    controller = TransportController(
        CAMERA,
        ProviderKind.FAKE,
        ControllerConfig(reconnect=ReconnectPolicy(initial_delay_seconds=0.05, jitter_ratio=0)),
    )
    backends: list[FakeBackend] = []

    def factory(generation: int) -> FakeBackend:
        backend = FakeBackend(generation, fail_after=0.3 if generation == 1 else None)
        backends.append(backend)
        return backend

    runner = CameraTransportRunner(controller, provider, factory, tick_interval_seconds=0.02)
    stop = asyncio.Event()
    with capture_logs() as logs:
        task = asyncio.create_task(runner.run(stop))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (  # noqa: ASYNC110 - polls thread-fed runner
            controller.current_generation != 2 or controller.state is not S.STREAMING
        ):
            await asyncio.sleep(0.02)
        stop.set()
        await task
    assert [b.generation for b in backends] == [1, 2]
    assert all(b.started and b.stopped for b in backends)
    assert controller.state is S.STOPPED
    assert controller.metrics.counters["camera_transport_reconnects_total"] == 1
    assert runner.handles == {}
    rendered = repr(logs) + "".join(b.lease_repr for b in backends)
    assert FAKE_SECRET not in rendered
    assert "synthetic-fake-camera" not in repr(logs)
    states = [
        entry["state"] for entry in logs if entry["event"] == "camera_transport_state_changed"
    ]
    assert states[:2] == ["CONNECTING", "STREAMING"]
    assert "RECONNECTING" in states and states[-1] == "STOPPED"


async def test_runner_maps_provider_timeout_and_offline() -> None:
    provider = FakeLiveSessionProvider(
        [FakeOutcome(delay_seconds=1.0), FakeOutcome(category=C.CAMERA_OFFLINE)],
        default=FakeOutcome(category=C.AUTHORIZATION_FAILED),
    )
    controller = TransportController(
        CAMERA,
        ProviderKind.FAKE,
        ControllerConfig(
            acquire_timeout_seconds=0.1,
            reconnect=ReconnectPolicy(initial_delay_seconds=0.02, jitter_ratio=0),
        ),
    )
    runner = CameraTransportRunner(controller, provider, FakeBackend, tick_interval_seconds=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.state is not S.FAILED:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    failures = dict(controller.metrics.failures)
    stop.set()
    await task
    assert failures[C.SESSION_ACQUIRE_TIMEOUT] == 1
    assert failures[C.CAMERA_OFFLINE] == 1
    assert failures[C.AUTHORIZATION_FAILED] == 1
    assert len(provider.calls) == 3
