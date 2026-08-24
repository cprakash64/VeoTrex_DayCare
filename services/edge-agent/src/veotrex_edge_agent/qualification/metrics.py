from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise

from veotrex_edge_agent.qualification.models import (
    ContinuitySummary,
    EngineeringTargets,
    QualificationDecision,
    SessionResult,
)


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    rank = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


@dataclass(slots=True)
class BoundedSamples:
    capacity: int = 2048
    _values: deque[float] = field(init=False)
    count: int = 0
    minimum: float | None = None
    maximum: float | None = None
    total: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        self._values = deque(maxlen=self.capacity)

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("sample must be finite")
        self._values.append(value)
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def retained_count(self) -> int:
        return len(self._values)

    def percentile(self, value: float) -> float | None:
        return percentile(self._values, value)

    def values(self) -> tuple[float, ...]:
        return tuple(self._values)


def continuity_summary(sessions: list[SessionResult]) -> ContinuitySummary:
    usable = [
        session
        for session in sessions
        if session.first_media_at is not None and session.last_media_at is not None
    ]
    gaps_seconds = [
        current.first_media_at - previous.last_media_at  # type: ignore[operator]
        for previous, current in pairwise(usable)
    ]
    gaps_ms = tuple(value * 1000 for value in gaps_seconds)
    positive_blind_seconds = sum(max(0.0, value) for value in gaps_seconds)
    if not usable:
        observation_window = 0.0
        availability = 0.0
    else:
        first = usable[0].first_media_at
        last = usable[-1].last_media_at
        assert first is not None and last is not None
        observation_window = max(0.0, last - first)
        availability = (
            max(0.0, observation_window - positive_blind_seconds) / observation_window * 100
            if observation_window > 0
            else 0.0
        )
    return ContinuitySummary(
        transition_gaps_ms=gaps_ms,
        minimum_gap_ms=min(gaps_ms) if gaps_ms else None,
        p50_gap_ms=percentile(gaps_ms, 50),
        p95_gap_ms=percentile(gaps_ms, 95),
        p99_gap_ms=percentile(gaps_ms, 99),
        maximum_gap_ms=max(gaps_ms) if gaps_ms else None,
        total_blind_time_seconds=positive_blind_seconds,
        media_availability_percent=availability,
        observation_window_seconds=observation_window,
    )


def decide(
    summary: ContinuitySummary,
    sessions: list[SessionResult],
    targets: EngineeringTargets,
) -> QualificationDecision:
    if len(sessions) < 2 or summary.p99_gap_ms is None:
        return QualificationDecision.INSUFFICIENT_DATA
    max_gap = max(0.0, summary.maximum_gap_ms or 0.0)
    if (
        summary.media_availability_percent >= targets.critical_availability_percent
        and summary.p99_gap_ms <= targets.critical_p99_gap_ms
        and max_gap <= targets.critical_max_blind_interval_ms
    ):
        return QualificationDecision.MEETS_STREAM_TARGET
    if (
        summary.media_availability_percent >= targets.operational_availability_percent
        and summary.p99_gap_ms <= targets.operational_p99_gap_ms
    ):
        return QualificationDecision.CONDITIONAL
    return QualificationDecision.DOES_NOT_MEET_STREAM_TARGET
