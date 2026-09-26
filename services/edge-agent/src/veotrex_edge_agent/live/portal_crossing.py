"""Anonymous room entry / exit from portal crossings (V1-05A).

``PERSON_ENTERED_ROOM`` and ``PERSON_EXITED_ROOM`` are *not* renamed track lifecycle facts. A
track starting is not an entry and a track ending is not an exit (``recorded.model``,
``timeline``). These two events are emitted only when an anonymous camera-session track is
seen to cross an operator-configured portal (``portal_geometry``) from one stable side to the
other, through the doorway.

**Per track, per portal, one small state machine** (``PortalTrackState``):

``UNKNOWN_SIDE``
    Nothing established yet. The first observation clearly on one side (outside the dead-band)
    *initialises* that side and emits nothing - a person already in the room when the stream
    starts, or first detected there, did not just enter it.
``INSIDE`` / ``OUTSIDE``
    The established side. An observation in the ``DEADBAND`` (within ``deadband`` of the line)
    is neutral: it neither confirms nor contradicts anything, so a person hovering in the
    doorway produces nothing at all.
Transition
    ``confirm_observations`` observations *in a row* on the opposite side (dead-band
    observations do not break the run; one back on the established side does) confirm a
    crossing. Together with the dead-band this means a transition needs a perpendicular movement
    of at least twice the dead-band and sustained evidence - one noisy box never fires.

**Through the doorway, not around it.** The side test uses the infinite line, so a crossing is
also checked against the segment: the point where the track's path (last point on the old
side -> first point on the new side) meets the line must lie on the portal, within
``segment_tolerance`` of its length. Otherwise the side is rebased silently and counted
(``outside_segment``) - someone who walked past the end of the doorway line did not use the
door.

**Cooldown delays, never hides.** After an event, the next one for the same track and portal
waits until ``cooldown_seconds`` of source time has passed. A confirmed opposite run is kept,
not discarded: if the person is still on the new side when the cooldown ends, the event is
emitted then; if they went back, nothing is emitted, which is the right answer for someone who
stepped in and straight back out of a doorway.

**What never emits.** Track birth (inside or outside), track death (inside or outside), a
discontinuity, a reconnect, a resolution change, a track the occupancy ledger has not validated,
a tentative track (never observed here at all). On any tracker discontinuity every portal state
is dropped and the tracks seen afterwards initialise afresh; a new track id always starts with
no state, and no id before a reset is ever linked to one after it.

Every event is anonymous and session-local: a portal, a track number, a direction and a time.
There is no roster, profile or contact id, no face, embedding, crop, image, clothing descriptor
or cross-camera identity anywhere in this module, and nothing here identifies anyone.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from veotrex_edge_agent.live.portal_geometry import Portal, PortalSet, track_reference_point
from veotrex_edge_agent.qualification.metrics import BoundedSamples

DEFAULT_CONFIRM_OBSERVATIONS = 3
DEFAULT_COOLDOWN_SECONDS = 1.0
DEFAULT_SEGMENT_TOLERANCE = 0.1
# The tracker's own bounds: 128 active + 128 lost tracks per stream.
MAX_TRACKED_PER_PORTAL = 256
MAX_RECENT_TRANSITIONS = 100
EVALUATION_SAMPLE_CAPACITY = 4096


class Side(StrEnum):
    UNKNOWN_SIDE = "UNKNOWN_SIDE"
    OUTSIDE = "OUTSIDE"
    INSIDE = "INSIDE"
    DEADBAND = "DEADBAND"


class RoomTransitionKind(StrEnum):
    PERSON_ENTERED_ROOM = "PERSON_ENTERED_ROOM"
    PERSON_EXITED_ROOM = "PERSON_EXITED_ROOM"


class Direction(StrEnum):
    OUTSIDE_TO_INSIDE = "OUTSIDE_TO_INSIDE"
    INSIDE_TO_OUTSIDE = "INSIDE_TO_OUTSIDE"


class CrossingOutcome(StrEnum):
    NONE = "NONE"
    INITIALISED = "INITIALISED"
    ENTERED = "ENTERED"
    EXITED = "EXITED"
    SUPPRESSED_NOT_VALIDATED = "SUPPRESSED_NOT_VALIDATED"
    OUTSIDE_SEGMENT = "OUTSIDE_SEGMENT"
    COOLDOWN_PENDING = "COOLDOWN_PENDING"
    OUT_OF_ORDER = "OUT_OF_ORDER"


@dataclass(frozen=True, slots=True)
class CrossingPolicy:
    confirm_observations: int = DEFAULT_CONFIRM_OBSERVATIONS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    segment_tolerance: float = DEFAULT_SEGMENT_TOLERANCE
    max_tracked_per_portal: int = MAX_TRACKED_PER_PORTAL

    def __post_init__(self) -> None:
        if isinstance(self.confirm_observations, bool) or not 1 <= self.confirm_observations <= 20:
            raise ValueError("confirm_observations must be between 1 and 20")
        if not 0.0 <= self.cooldown_seconds <= 60.0:
            raise ValueError("cooldown_seconds must be between 0 and 60")
        if not 0.0 <= self.segment_tolerance <= 1.0:
            raise ValueError("segment_tolerance must be between 0 and 1")
        if not 1 <= self.max_tracked_per_portal <= 4096:
            raise ValueError("max_tracked_per_portal must be between 1 and 4096")


@dataclass(slots=True)
class PortalTrackState:
    """Everything one portal remembers about one track: O(1), no history list."""

    side: Side = Side.UNKNOWN_SIDE
    last_side_point: tuple[float, float] | None = None
    candidate: Side | None = None
    candidate_count: int = 0
    candidate_first_point: tuple[float, float] | None = None
    last_timestamp_ms: float | None = None
    last_event_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RoomTransition:
    """One anonymous, session-local crossing. No identity of any kind."""

    sequence: int
    kind: RoomTransitionKind
    direction: Direction
    stream_id: str
    track_id: int
    portal_id: str
    portal_label: str
    timestamp_ms: float
    crossing_point: tuple[float, float]
    evidence_observations: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": str(self.kind),
            "direction": str(self.direction),
            "stream_id": self.stream_id,
            "track_id": self.track_id,
            "portal_id": self.portal_id,
            "portal_label": self.portal_label,
            "timestamp_ms": round(self.timestamp_ms, 1),
            "crossing_point": [round(value, 4) for value in self.crossing_point],
            "evidence_observations": self.evidence_observations,
        }


@dataclass(frozen=True, slots=True)
class CrossingResult:
    outcome: CrossingOutcome
    direction: Direction | None = None
    crossing_point: tuple[float, float] | None = None
    evidence_observations: int = 0


def classify(portal: Portal, point: tuple[float, float]) -> Side:
    offset = portal.inside_offset(point)
    if offset > portal.deadband:
        return Side.INSIDE
    if offset < -portal.deadband:
        return Side.OUTSIDE
    return Side.DEADBAND


def advance(
    portal: Portal,
    state: PortalTrackState,
    point: tuple[float, float],
    timestamp_ms: float,
    policy: CrossingPolicy,
    *,
    eligible: bool,
) -> CrossingResult:
    """Feed one observation of one track to one portal's state. Pure apart from ``state``.

    ``eligible`` is whether this track may produce an event right now (the occupancy ledger has
    validated it). An ineligible confirmed crossing still moves the state to the new side, so
    it cannot fire later as a delayed or duplicate event.
    """
    if state.last_timestamp_ms is not None and timestamp_ms < state.last_timestamp_ms:
        return CrossingResult(CrossingOutcome.OUT_OF_ORDER)
    state.last_timestamp_ms = timestamp_ms
    side = classify(portal, point)
    if side is Side.DEADBAND:
        return CrossingResult(CrossingOutcome.NONE)
    if state.side is Side.UNKNOWN_SIDE:
        state.side, state.last_side_point = side, point
        return CrossingResult(CrossingOutcome.INITIALISED)
    if side is state.side:
        state.last_side_point = point
        state.candidate, state.candidate_count, state.candidate_first_point = None, 0, None
        return CrossingResult(CrossingOutcome.NONE)
    if state.candidate is not side:
        state.candidate, state.candidate_count, state.candidate_first_point = side, 0, point
    state.candidate_count += 1
    if state.candidate_count < policy.confirm_observations:
        return CrossingResult(CrossingOutcome.NONE)
    if (
        state.last_event_ms is not None
        and timestamp_ms - state.last_event_ms < policy.cooldown_seconds * 1000.0
    ):
        return CrossingResult(CrossingOutcome.COOLDOWN_PENDING)
    before, after = state.last_side_point, state.candidate_first_point
    assert before is not None and after is not None
    evidence = state.candidate_count
    direction = Direction.OUTSIDE_TO_INSIDE if side is Side.INSIDE else Direction.INSIDE_TO_OUTSIDE
    # Rebase first: whatever the outcome, the track is now established on the new side.
    state.side, state.last_side_point = side, point
    state.candidate, state.candidate_count, state.candidate_first_point = None, 0, None
    crossing = _crossing_point(portal, before, after)
    tolerance = policy.segment_tolerance
    if crossing is None or not -tolerance <= portal.segment_parameter(crossing) <= 1 + tolerance:
        return CrossingResult(CrossingOutcome.OUTSIDE_SEGMENT, direction)
    if not eligible:
        return CrossingResult(
            CrossingOutcome.SUPPRESSED_NOT_VALIDATED, direction, crossing, evidence
        )
    state.last_event_ms = timestamp_ms
    outcome = CrossingOutcome.ENTERED if side is Side.INSIDE else CrossingOutcome.EXITED
    return CrossingResult(outcome, direction, crossing, evidence)


def _crossing_point(
    portal: Portal, before: tuple[float, float], after: tuple[float, float]
) -> tuple[float, float] | None:
    """Where the straight path ``before -> after`` meets the portal's line."""
    first, second = portal.inside_offset(before), portal.inside_offset(after)
    if first == second:
        return None
    ratio = first / (first - second)
    return (
        before[0] + ratio * (after[0] - before[0]),
        before[1] + ratio * (after[1] - before[1]),
    )


@dataclass(slots=True)
class PortalCounters:
    entries: int = 0
    exits: int = 0
    suppressed_not_validated: int = 0
    outside_segment: int = 0
    cooldown_deferrals: int = 0
    out_of_order: int = 0
    evictions: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "entries": self.entries,
            "exits": self.exits,
            "suppressed_not_validated": self.suppressed_not_validated,
            "outside_segment": self.outside_segment,
            "cooldown_deferrals": self.cooldown_deferrals,
            "out_of_order": self.out_of_order,
            "evictions": self.evictions,
        }


@dataclass(slots=True)
class _PortalRuntime:
    portal: Portal
    tracks: OrderedDict[int, PortalTrackState] = field(default_factory=OrderedDict)
    counters: PortalCounters = field(default_factory=PortalCounters)


class PortalMonitor:
    """All enabled portals of one camera session, with bounded state and a bounded event log.

    Not thread-safe by itself: the live runtime calls ``observe``/``forget``/``reset`` from its
    single pipeline thread and reads ``snapshot`` under its own lock-free copy semantics (every
    value returned is a fresh dict).
    """

    def __init__(
        self,
        portals: PortalSet,
        *,
        stream_id: str,
        policy: CrossingPolicy | None = None,
        max_recent: int = MAX_RECENT_TRANSITIONS,
    ) -> None:
        self.portals = portals
        self.policy = policy or CrossingPolicy()
        self.stream_id = stream_id
        self._runtimes = [_PortalRuntime(portal) for portal in portals.enabled]
        self._recent: deque[RoomTransition] = deque(maxlen=max_recent)
        self._sequence = 0
        self.resets_total = 0
        self.skipped_observations_total = 0
        self.evaluation_us = BoundedSamples(EVALUATION_SAMPLE_CAPACITY)

    def __bool__(self) -> bool:
        return bool(self._runtimes)

    def observe(
        self,
        track_id: int,
        bbox_xyxy: tuple[float, ...],
        *,
        width: int,
        height: int,
        timestamp_ms: float,
        eligible: bool,
    ) -> list[RoomTransition]:
        """One CONFIRMED observation of one track, against every enabled portal, in order."""
        if not self._runtimes:
            return []
        started = time.perf_counter_ns()
        point = track_reference_point(bbox_xyxy, width=width, height=height)
        emitted: list[RoomTransition] = []
        if point is None:
            self.skipped_observations_total += 1
            return emitted
        for runtime in self._runtimes:
            state = runtime.tracks.get(track_id)
            if state is None:
                state = PortalTrackState()
                runtime.tracks[track_id] = state
                while len(runtime.tracks) > self.policy.max_tracked_per_portal:
                    runtime.tracks.popitem(last=False)
                    runtime.counters.evictions += 1
            else:
                runtime.tracks.move_to_end(track_id)
            result = advance(
                runtime.portal, state, point, timestamp_ms, self.policy, eligible=eligible
            )
            counters = runtime.counters
            if result.outcome is CrossingOutcome.OUT_OF_ORDER:
                counters.out_of_order += 1
            elif result.outcome is CrossingOutcome.OUTSIDE_SEGMENT:
                counters.outside_segment += 1
            elif result.outcome is CrossingOutcome.COOLDOWN_PENDING:
                counters.cooldown_deferrals += 1
            elif result.outcome is CrossingOutcome.SUPPRESSED_NOT_VALIDATED:
                counters.suppressed_not_validated += 1
            elif result.outcome in (CrossingOutcome.ENTERED, CrossingOutcome.EXITED):
                assert result.direction is not None and result.crossing_point is not None
                entered = result.outcome is CrossingOutcome.ENTERED
                if entered:
                    counters.entries += 1
                else:
                    counters.exits += 1
                self._sequence += 1
                event = RoomTransition(
                    sequence=self._sequence,
                    kind=RoomTransitionKind.PERSON_ENTERED_ROOM
                    if entered
                    else RoomTransitionKind.PERSON_EXITED_ROOM,
                    direction=result.direction,
                    stream_id=self.stream_id,
                    track_id=int(track_id),
                    portal_id=runtime.portal.portal_id,
                    portal_label=runtime.portal.label,
                    timestamp_ms=float(timestamp_ms),
                    crossing_point=result.crossing_point,
                    evidence_observations=result.evidence_observations,
                )
                self._recent.append(event)
                emitted.append(event)
        self.evaluation_us.add((time.perf_counter_ns() - started) / 1e3)
        return emitted

    def forget(self, track_id: int) -> None:
        """A track ended. Its death is not an exit: the state is simply dropped."""
        for runtime in self._runtimes:
            runtime.tracks.pop(track_id, None)

    def reset(self) -> None:
        """A discontinuity: every state goes; tracks seen afterwards initialise afresh."""
        for runtime in self._runtimes:
            runtime.tracks.clear()
        self.resets_total += 1

    def tracked(self) -> int:
        return sum(len(runtime.tracks) for runtime in self._runtimes)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent first, bounded by ``limit``."""
        return [event.as_dict() for event in list(self._recent)[-limit:][::-1]]

    def snapshot(self, *, recent_limit: int = 20) -> dict[str, Any]:
        per_portal = {
            runtime.portal.portal_id: {
                **runtime.counters.as_dict(),
                "tracked": len(runtime.tracks),
            }
            for runtime in self._runtimes
        }
        return {
            "configured": len(self.portals),
            "enabled": len(self._runtimes),
            "portals": self.portals.as_dicts(),
            "entries_total": sum(r.counters.entries for r in self._runtimes),
            "exits_total": sum(r.counters.exits for r in self._runtimes),
            "per_portal": per_portal,
            "recent": self.recent(recent_limit),
            "resets_total": self.resets_total,
            "skipped_observations_total": self.skipped_observations_total,
            "tracked_states": self.tracked(),
            "policy": {
                "confirm_observations": self.policy.confirm_observations,
                "cooldown_seconds": self.policy.cooldown_seconds,
                "segment_tolerance": self.policy.segment_tolerance,
                "max_tracked_per_portal": self.policy.max_tracked_per_portal,
            },
            "evaluation_us": {
                "count": float(self.evaluation_us.count),
                "p50": self.evaluation_us.percentile(50),
                "p95": self.evaluation_us.percentile(95),
                "max": self.evaluation_us.maximum,
            },
        }
