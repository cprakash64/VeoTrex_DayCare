from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum


class TransportState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    STREAMING = "STREAMING"
    DEGRADED = "DEGRADED"
    RENEWING = "RENEWING"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


_S = TransportState
ALLOWED_TRANSITIONS: dict[TransportState, frozenset[TransportState]] = {
    _S.STOPPED: frozenset({_S.CONNECTING}),
    _S.CONNECTING: frozenset({_S.STREAMING, _S.RECONNECTING, _S.FAILED, _S.STOPPED}),
    _S.STREAMING: frozenset({_S.DEGRADED, _S.RENEWING, _S.RECONNECTING, _S.FAILED, _S.STOPPED}),
    _S.DEGRADED: frozenset({_S.STREAMING, _S.RENEWING, _S.RECONNECTING, _S.FAILED, _S.STOPPED}),
    # CONNECTING from RENEWING: the old session expired before its replacement produced media.
    _S.RENEWING: frozenset(
        {_S.STREAMING, _S.DEGRADED, _S.CONNECTING, _S.RECONNECTING, _S.FAILED, _S.STOPPED}
    ),
    _S.RECONNECTING: frozenset({_S.CONNECTING, _S.FAILED, _S.STOPPED}),
    # FAILED is terminal (circuit open or non-retryable) until an explicit operator reset/stop.
    _S.FAILED: frozenset({_S.STOPPED}),
}


class InvalidTransportTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StateTransition:
    previous: TransportState
    state: TransportState
    category: str
    at: float
    generation: int | None


class TransportStateMachine:
    def __init__(self, history_capacity: int = 128) -> None:
        self._state = TransportState.STOPPED
        self._history: deque[StateTransition] = deque(maxlen=history_capacity)
        self.transition_count = 0

    @property
    def state(self) -> TransportState:
        return self._state

    @property
    def history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    def transition(
        self,
        state: TransportState,
        *,
        category: str,
        at: float,
        generation: int | None = None,
    ) -> StateTransition | None:
        if state is self._state:
            return None
        if state not in ALLOWED_TRANSITIONS[self._state]:
            raise InvalidTransportTransition(f"{self._state.value}->{state.value}")
        record = StateTransition(self._state, state, category, at, generation)
        self._state = state
        self._history.append(record)
        self.transition_count += 1
        return record
