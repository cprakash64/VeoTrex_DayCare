from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Bounded exponential backoff with jitter and a sliding-window circuit breaker.

    Defaults are R5A qualification values, not tuned production constants: worst case is
    ``max_attempts`` sessions per ``window_seconds`` per camera before the circuit opens.
    """

    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2
    max_attempts: int = 8
    window_seconds: float = 600.0
    stable_reset_seconds: float = 60.0

    def __post_init__(self) -> None:
        values = (
            self.initial_delay_seconds,
            self.maximum_delay_seconds,
            self.multiplier,
            self.jitter_ratio,
            self.window_seconds,
            self.stable_reset_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("reconnect policy values must be finite")
        if not 0 < self.initial_delay_seconds <= self.maximum_delay_seconds <= 3600:
            raise ValueError("reconnect delays must satisfy 0 < initial <= maximum <= 3600")
        if not 1 <= self.multiplier <= 10:
            raise ValueError("reconnect multiplier must be between 1 and 10")
        if not 0 <= self.jitter_ratio < 1:
            raise ValueError("jitter ratio must be in [0, 1)")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max attempts must be between 1 and 100")
        if not 1 <= self.window_seconds <= 86_400:
            raise ValueError("reconnect window is out of bounds")
        if self.stable_reset_seconds <= 0:
            raise ValueError("stable reset must be positive")


class ReconnectBudget:
    def __init__(
        self, policy: ReconnectPolicy, random_value: Callable[[], float] = random.random
    ) -> None:
        self.policy = policy
        self._random = random_value
        self._attempts: deque[float] = deque()
        self.consecutive_failures = 0

    @property
    def attempts_in_window(self) -> int:
        return len(self._attempts)

    def _prune(self, now: float) -> None:
        while self._attempts and now - self._attempts[0] >= self.policy.window_seconds:
            self._attempts.popleft()

    def next_delay(self, now: float) -> float | None:
        """Return the delay before the next attempt, or None when the circuit is open."""
        self._prune(now)
        if len(self._attempts) >= self.policy.max_attempts:
            return None
        base = min(
            self.policy.maximum_delay_seconds,
            self.policy.initial_delay_seconds
            * self.policy.multiplier ** min(self.consecutive_failures, 32),
        )
        jitter = base * self.policy.jitter_ratio * (2 * self._random() - 1)
        delay = min(self.policy.maximum_delay_seconds, max(0.0, base + jitter))
        self._attempts.append(now)
        self.consecutive_failures += 1
        return delay

    def reset(self) -> None:
        """Documented reset: after stable streaming or an explicit operator reset."""
        self._attempts.clear()
        self.consecutive_failures = 0
