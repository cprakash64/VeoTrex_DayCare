from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from pydantic import SecretStr

from veotrex_edge_agent.qualification.models import (
    SessionRequest,
    SessionResult,
    TerminationReason,
)


@dataclass(frozen=True, slots=True)
class FakeSessionPlan:
    requested_at: float
    first_media_at: float | None
    last_media_at: float | None
    ended_at: float
    codec: str = "H264"
    decoder: str | None = None
    encoded_bytes: int = 1000
    media_buffers: int = 10
    decoded_frames: int = 0
    termination_reason: TerminationReason = TerminationReason.PROVIDER_SESSION_EXPIRATION
    failure_category: str | None = None
    wall_delay_seconds: float = 0.0
    reject_when_concurrent: bool = False


class FakeStreamBackend:
    """Deterministic CI backend covering transport, codec, lifecycle, and failures."""

    def __init__(self, plans: list[FakeSessionPlan]) -> None:
        self._plans = deque(plans)
        self.calls = 0
        self.maximum_active = 0
        self._active = 0
        self._lock = asyncio.Lock()

    async def run_session(
        self,
        request: SessionRequest,
        access_token: SecretStr,
        cancel: asyncio.Event,
    ) -> SessionResult:
        _ = access_token  # Exercise the typed boundary without unwrapping the fake credential.
        async with self._lock:
            if not self._plans:
                raise RuntimeError("fake plan exhausted")
            plan = self._plans.popleft()
            concurrent = self._active > 0
            self._active += 1
            self.maximum_active = max(self.maximum_active, self._active)
            self.calls += 1
        try:
            if plan.wall_delay_seconds:
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=plan.wall_delay_seconds)
                except TimeoutError:
                    pass
            if cancel.is_set():
                return SessionResult(
                    request.session_number,
                    plan.requested_at,
                    ended_at=plan.requested_at,
                    teardown_completed_at=plan.requested_at,
                    termination_reason=TerminationReason.CANCELLED,
                    failure_category="cancelled",
                )
            if plan.reject_when_concurrent and concurrent:
                return SessionResult(
                    request.session_number,
                    plan.requested_at,
                    connection_started_at=plan.requested_at,
                    ended_at=plan.requested_at,
                    teardown_completed_at=plan.requested_at,
                    termination_reason=TerminationReason.CONCURRENT_SESSION_REJECTED,
                    failure_category="concurrent_session_rejected",
                )
            first_decoded = (
                plan.first_media_at
                if plan.decoded_frames and plan.first_media_at is not None
                else None
            )
            return SessionResult(
                session_number=request.session_number,
                requested_at=plan.requested_at,
                connection_started_at=plan.requested_at + 0.01,
                describe_completed_at=plan.requested_at + 0.02,
                play_started_at=plan.requested_at + 0.03,
                first_media_at=plan.first_media_at,
                first_decoded_frame_at=first_decoded,
                last_media_at=plan.last_media_at,
                last_decoded_frame_at=plan.last_media_at if plan.decoded_frames else None,
                ended_at=plan.ended_at,
                teardown_completed_at=plan.ended_at + 0.01,
                codec=plan.codec,
                decoder=plan.decoder,
                media_buffers=plan.media_buffers,
                decoded_frames=plan.decoded_frames,
                encoded_bytes=plan.encoded_bytes,
                frame_gap_samples_ms=[33.3] * min(plan.media_buffers, 32),
                max_frame_gap_ms=33.3 if plan.media_buffers else None,
                termination_reason=plan.termination_reason,
                failure_category=plan.failure_category,
            )
        finally:
            async with self._lock:
                self._active -= 1


class FakeTokenProvider:
    def __init__(self, tokens: list[str] | None = None, *, fail_after: int | None = None) -> None:
        self._tokens = tokens or ["fake-token"]
        self._fail_after = fail_after
        self.calls = 0

    async def access_token_for(self, _request: SessionRequest) -> SecretStr:
        if self._fail_after is not None and self.calls >= self._fail_after:
            raise RuntimeError("credential_unavailable")
        value = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return SecretStr(value)
