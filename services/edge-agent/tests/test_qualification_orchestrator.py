import asyncio
from uuid import UUID

import pytest

from veotrex_edge_agent.qualification.fakes import (
    FakeSessionPlan,
    FakeStreamBackend,
    FakeTokenProvider,
)
from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
    TerminationReason,
)
from veotrex_edge_agent.qualification.orchestrator import (
    QualificationOrchestrator,
    SequentialConfig,
)


def target(number: int = 1) -> CameraTarget:
    return CameraTarget(
        UUID(int=number), f"camera-{number:02d}", f"private-provider-{number}", None
    )


def config(
    *, cycles: int = 2, retries: int = 0, session_class: SessionClass | None = None
) -> SequentialConfig:
    return SequentialConfig(
        QualificationMode.TRANSPORT,
        session_class or SessionClass.LINE_POWERED_60_SECONDS,
        cycles=cycles,
        max_retries_per_session=retries,
    )


def plan(
    requested: float,
    first: float | None,
    last: float | None,
    *,
    reason: TerminationReason = TerminationReason.PROVIDER_SESSION_EXPIRATION,
    failure: str | None = None,
    delay: float = 0,
    reject_when_concurrent: bool = False,
) -> FakeSessionPlan:
    return FakeSessionPlan(
        requested,
        first,
        last,
        (last or requested) + 0.1,
        termination_reason=reason,
        failure_category=failure,
        wall_delay_seconds=delay,
        reject_when_concurrent=reject_when_concurrent,
    )


async def test_sequential_reconnect_obtains_fresh_token_before_every_session() -> None:
    backend = FakeStreamBackend([plan(0, 0.1, 60), plan(60, 60.2, 120)])
    tokens = FakeTokenProvider(["first", "refreshed"])
    result = await QualificationOrchestrator(backend, tokens).run_sequential(target(), config())
    assert len(result.sessions) == 2
    assert result.continuity.transition_gaps_ms == pytest.approx((200.0,))
    assert tokens.calls == 2
    assert result.complete


@pytest.mark.parametrize(
    ("session_class", "duration"),
    [
        (SessionClass.BATTERY_30_SECONDS, 30),
        (SessionClass.LINE_POWERED_60_SECONDS, 60),
    ],
)
async def test_provider_duration_classes_are_explicit(
    session_class: SessionClass, duration: int
) -> None:
    backend = FakeStreamBackend([plan(0, 0.1, float(duration))])
    result = await QualificationOrchestrator(backend, FakeTokenProvider()).run_sequential(
        target(), config(cycles=1, session_class=session_class)
    )
    assert result.session_class.expected_limit_seconds == duration
    assert result.sessions[0].observed_duration_seconds == pytest.approx(duration + 0.09)


async def test_retry_is_bounded_and_categorized() -> None:
    backend = FakeStreamBackend(
        [
            plan(
                0, None, None, reason=TerminationReason.NETWORK_DISCONNECT, failure="tcp_disconnect"
            ),
            plan(1, None, None, reason=TerminationReason.TLS_FAILURE, failure="tls_failure"),
        ]
    )
    result = await QualificationOrchestrator(backend, FakeTokenProvider()).run_sequential(
        target(), config(cycles=1, retries=1)
    )
    assert backend.calls == 2
    assert result.failures == ["tls_failure"]


async def test_overlap_success_preserves_negative_resulting_gap() -> None:
    backend = FakeStreamBackend([plan(0, 0.1, 60, delay=0.02), plan(55, 55.1, 115, delay=0.01)])
    orchestrator = QualificationOrchestrator(backend, FakeTokenProvider())

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    first, second, overlap = await orchestrator.run_overlap_experiment(
        target(), config(), lead_seconds=5, sleep=no_wait
    )
    assert first.first_media_at == 0.1
    assert second.first_media_at == 55.1
    assert overlap.supported_observed == "yes"
    assert overlap.resulting_gap_ms == pytest.approx(-4900)
    assert overlap.overlap_duration_seconds == pytest.approx(4.9)


async def test_overlap_rejection_is_reported_without_workaround() -> None:
    backend = FakeStreamBackend(
        [
            plan(0, 0.1, 60, delay=0.02),
            plan(55, None, None, delay=0.01, reject_when_concurrent=True),
        ]
    )

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    _, second, overlap = await QualificationOrchestrator(
        backend, FakeTokenProvider()
    ).run_overlap_experiment(target(), config(), lead_seconds=5, sleep=no_wait)
    assert second.termination_reason is TerminationReason.CONCURRENT_SESSION_REJECTED
    assert overlap.second_session_status == "rejected"
    assert overlap.supported_observed == "no"


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8, 12])
async def test_explicit_supported_concurrency_levels(concurrency: int) -> None:
    plans = [
        plan(float(index), float(index), float(index + 1), delay=0.01)
        for index in range(concurrency)
    ]
    backend = FakeStreamBackend(plans)
    results = await QualificationOrchestrator(backend, FakeTokenProvider()).run_concurrency(
        [target(index + 1) for index in range(concurrency)],
        config(cycles=1),
        concurrency=concurrency,
        ramp_seconds=0,
    )
    assert len(results) == concurrency
    assert backend.maximum_active == concurrency


async def test_concurrency_requires_exact_explicit_target_count() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        await QualificationOrchestrator(FakeStreamBackend([]), FakeTokenProvider()).run_concurrency(
            [target()], config(cycles=1), concurrency=2, ramp_seconds=0
        )


async def test_graceful_cancellation_marks_partial_run_incomplete() -> None:
    cancel = asyncio.Event()
    backend = FakeStreamBackend([plan(0, 0.1, 60, delay=1)])
    task = asyncio.create_task(
        QualificationOrchestrator(backend, FakeTokenProvider()).run_sequential(
            target(), config(cycles=3), cancel
        )
    )
    await asyncio.sleep(0)
    cancel.set()
    result = await task
    assert not result.complete
    assert result.sessions[0].termination_reason is TerminationReason.CANCELLED


async def test_unsafe_credential_state_fails_closed_before_backend() -> None:
    backend = FakeStreamBackend([plan(0, 0.1, 1)])
    with pytest.raises(RuntimeError, match="credential_unavailable"):
        await QualificationOrchestrator(backend, FakeTokenProvider(fail_after=0)).run_sequential(
            target(), config(cycles=1)
        )
    assert backend.calls == 0
