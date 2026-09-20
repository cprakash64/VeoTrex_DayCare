"""Session safety events derived from real state transitions.

Every event here is produced by the running pipeline observing its own state change - a
coverage loss that actually happened, a head count that actually crossed a configured
threshold. Nothing is seeded, scheduled or replayed, so an uneventful session produces an
empty list and the dashboard says so.

Events live in memory for the life of the process. There is no event table in the control
plane yet, so these are explicitly session-scoped rather than a durable safety record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from veotrex_edge_agent.monitoring.occupancy import (
    CoverageState,
    OccupancyReading,
    ThresholdState,
)

MAX_EVENTS = 200


class SafetyEventKind(StrEnum):
    MONITORING_COVERAGE_LOST = "MONITORING_COVERAGE_LOST"
    MONITORING_COVERAGE_RESTORED = "MONITORING_COVERAGE_RESTORED"
    DEMO_THRESHOLD_EXCEEDED = "DEMO_THRESHOLD_EXCEEDED"
    DEMO_THRESHOLD_CLEARED = "DEMO_THRESHOLD_CLEARED"


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    sequence: int
    kind: SafetyEventKind
    area_label: str
    occurred_at: str
    occurred_at_monotonic: float
    people_detected: int | None
    permitted_people: int | None
    # Set when this event closes a condition that had a measurable duration.
    duration_seconds: float | None


class SessionEventLog:
    """Bounded transition detector. One instance per monitored area."""

    def __init__(self, area_label: str, *, max_events: int = MAX_EVENTS) -> None:
        self._area_label = area_label
        self._events: deque[SafetyEvent] = deque(maxlen=max_events)
        self._sequence = 0
        self._coverage: CoverageState | None = None
        self._threshold: ThresholdState | None = None
        self._coverage_lost_at: float | None = None
        self._threshold_exceeded_at: float | None = None

    def events(self) -> tuple[SafetyEvent, ...]:
        """Newest first, which is the order a timeline reads in."""
        return tuple(reversed(self._events))

    def observe(self, reading: OccupancyReading) -> tuple[SafetyEvent, ...]:
        emitted: list[SafetyEvent] = []
        emitted.extend(self._observe_coverage(reading))
        emitted.extend(self._observe_threshold(reading))
        self._coverage = reading.coverage
        self._threshold = reading.threshold_state
        return tuple(emitted)

    def _observe_coverage(self, reading: OccupancyReading) -> list[SafetyEvent]:
        previous, current = self._coverage, reading.coverage
        if previous is None or previous == current:
            return []
        at = reading.observed_at_monotonic
        if previous is CoverageState.ACTIVE:
            self._coverage_lost_at = at
            return [self._record(SafetyEventKind.MONITORING_COVERAGE_LOST, reading, None)]
        if current is CoverageState.ACTIVE:
            duration = None if self._coverage_lost_at is None else at - self._coverage_lost_at
            self._coverage_lost_at = None
            return [self._record(SafetyEventKind.MONITORING_COVERAGE_RESTORED, reading, duration)]
        return []

    def _observe_threshold(self, reading: OccupancyReading) -> list[SafetyEvent]:
        previous, current = self._threshold, reading.threshold_state
        if previous == current:
            return []
        at = reading.observed_at_monotonic
        if current is ThresholdState.OVER_THRESHOLD:
            self._threshold_exceeded_at = at
            return [self._record(SafetyEventKind.DEMO_THRESHOLD_EXCEEDED, reading, None)]
        if previous is ThresholdState.OVER_THRESHOLD and current is ThresholdState.WITHIN_THRESHOLD:
            started = self._threshold_exceeded_at
            duration = None if started is None else at - started
            self._threshold_exceeded_at = None
            return [self._record(SafetyEventKind.DEMO_THRESHOLD_CLEARED, reading, duration)]
        # Falling into UNKNOWN is a coverage story, already reported as a coverage event.
        return []

    def _record(
        self, kind: SafetyEventKind, reading: OccupancyReading, duration: float | None
    ) -> SafetyEvent:
        event = SafetyEvent(
            sequence=self._sequence,
            kind=kind,
            area_label=self._area_label,
            occurred_at=datetime.now(UTC).isoformat(timespec="seconds"),
            occurred_at_monotonic=reading.observed_at_monotonic,
            people_detected=reading.people_detected,
            permitted_people=reading.permitted_people,
            duration_seconds=None if duration is None else round(duration, 1),
        )
        self._sequence += 1
        self._events.append(event)
        return event
