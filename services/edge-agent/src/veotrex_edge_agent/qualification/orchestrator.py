from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from veotrex_edge_agent.qualification.backend import AccessTokenProvider, StreamBackend
from veotrex_edge_agent.qualification.metrics import continuity_summary, decide
from veotrex_edge_agent.qualification.models import (
    CameraQualificationResult,
    CameraTarget,
    EngineeringTargets,
    OverlapResult,
    QualificationMode,
    SessionClass,
    SessionRequest,
    SessionResult,
    TerminationReason,
)

SUPPORTED_CONCURRENCY = frozenset({1, 2, 4, 8, 12})


@dataclass(frozen=True, slots=True)
class SequentialConfig:
    mode: QualificationMode
    session_class: SessionClass
    cycles: int = 3
    max_retries_per_session: int = 1
    decoder_preference: str = "auto"
    stall_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1 <= self.cycles <= 10_000:
            raise ValueError("cycles must be between 1 and 10000")
        if not 0 <= self.max_retries_per_session <= 3:
            raise ValueError("retries must be between 0 and 3")


class QualificationOrchestrator:
    def __init__(
        self,
        backend: StreamBackend,
        tokens: AccessTokenProvider,
        *,
        targets: EngineeringTargets | None = None,
    ) -> None:
        self._backend = backend
        self._tokens = tokens
        self._targets = targets or EngineeringTargets()

    @property
    def targets(self) -> EngineeringTargets:
        return self._targets

    async def run_sequential(
        self,
        target: CameraTarget,
        config: SequentialConfig,
        cancel: asyncio.Event | None = None,
    ) -> CameraQualificationResult:
        stop = cancel or asyncio.Event()
        sessions: list[SessionResult] = []
        failures: list[str] = []
        complete = True
        for session_number in range(1, config.cycles + 1):
            if stop.is_set():
                complete = False
                break
            request = SessionRequest(
                target=target,
                mode=config.mode,
                session_class=config.session_class,
                session_number=session_number,
                decoder_preference=config.decoder_preference,
                stall_timeout_seconds=config.stall_timeout_seconds,
            )
            result = await self._run_with_bounded_retries(request, config, stop)
            sessions.append(result)
            if result.failure_category:
                failures.append(result.failure_category)
            if result.termination_reason is TerminationReason.CANCELLED:
                complete = False
                break
        summary = continuity_summary(sessions)
        return CameraQualificationResult(
            camera_id=target.camera_id,
            label=target.label,
            mode=config.mode,
            session_class=config.session_class,
            complete=complete and len(sessions) == config.cycles,
            sessions=sessions,
            continuity=summary,
            decision=decide(summary, sessions, self._targets),
            failures=failures,
        )

    async def _run_with_bounded_retries(
        self,
        request: SessionRequest,
        config: SequentialConfig,
        cancel: asyncio.Event,
    ) -> SessionResult:
        result: SessionResult | None = None
        for attempt in range(config.max_retries_per_session + 1):
            if cancel.is_set():
                return SessionResult(
                    session_number=request.session_number,
                    requested_at=0.0,
                    termination_reason=TerminationReason.CANCELLED,
                    failure_category="cancelled",
                )
            token = await self._tokens.access_token_for(request)
            result = await self._backend.run_session(request, token, cancel)
            if result.termination_reason not in {
                TerminationReason.NETWORK_DISCONNECT,
                TerminationReason.TLS_FAILURE,
                TerminationReason.CAMERA_OFFLINE,
            }:
                return result
            if attempt < config.max_retries_per_session:
                await asyncio.sleep(min(2**attempt, 2))
        assert result is not None
        return result

    async def run_overlap_experiment(
        self,
        target: CameraTarget,
        config: SequentialConfig,
        *,
        lead_seconds: float,
        cancel: asyncio.Event | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> tuple[SessionResult, SessionResult, OverlapResult]:
        if not 0 < lead_seconds < config.session_class.expected_limit_seconds:
            raise ValueError("overlap lead must be positive and below the expected session limit")
        stop = cancel or asyncio.Event()
        request_a = SessionRequest(
            target,
            config.mode,
            config.session_class,
            1,
            config.decoder_preference,
            config.stall_timeout_seconds,
        )
        request_b = replace(request_a, session_number=2)
        token_a = await self._tokens.access_token_for(request_a)
        task_a = asyncio.create_task(self._backend.run_session(request_a, token_a, stop))
        try:
            await sleep(config.session_class.expected_limit_seconds - lead_seconds)
            token_b = await self._tokens.access_token_for(request_b)
            task_b = asyncio.create_task(self._backend.run_session(request_b, token_b, stop))
            result_b = await task_b
            result_a = await task_a
        except BaseException:
            stop.set()
            task_a.cancel()
            await asyncio.gather(task_a, return_exceptions=True)
            raise
        accepted = result_b.first_media_at is not None
        overlap_seconds: float | None = None
        resulting_gap_ms: float | None = None
        if result_a.last_media_at is not None and result_b.first_media_at is not None:
            resulting_gap_ms = (result_b.first_media_at - result_a.last_media_at) * 1000
            overlap_seconds = max(0.0, -resulting_gap_ms / 1000)
        overlap = OverlapResult(
            attempted=True,
            supported_observed=("yes" if accepted and (overlap_seconds or 0) > 0 else "no"),
            second_session_status=("accepted" if accepted else "rejected"),
            first_frame_b_at=result_b.first_media_at,
            last_frame_a_at=result_a.last_media_at,
            overlap_duration_seconds=overlap_seconds,
            disruption_to_a=result_a.termination_reason
            is TerminationReason.CONCURRENT_SESSION_REJECTED,
            resulting_gap_ms=resulting_gap_ms,
        )
        return result_a, result_b, overlap

    async def run_concurrency(
        self,
        targets: Sequence[CameraTarget],
        config: SequentialConfig,
        *,
        concurrency: int,
        ramp_seconds: float = 1.0,
        cancel: asyncio.Event | None = None,
    ) -> list[CameraQualificationResult]:
        if concurrency not in SUPPORTED_CONCURRENCY:
            raise ValueError("concurrency must be one of 1, 2, 4, 8, or 12")
        if len(targets) != concurrency:
            raise ValueError("target count must exactly match explicit concurrency")
        if not 0 <= ramp_seconds <= 60:
            raise ValueError("ramp must be between 0 and 60 seconds")
        stop = cancel or asyncio.Event()

        async def launch(index: int, target: CameraTarget) -> CameraQualificationResult:
            if index and ramp_seconds:
                await asyncio.sleep(index * ramp_seconds)
            return await self.run_sequential(target, config, stop)

        tasks = [asyncio.create_task(launch(index, target)) for index, target in enumerate(targets)]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
