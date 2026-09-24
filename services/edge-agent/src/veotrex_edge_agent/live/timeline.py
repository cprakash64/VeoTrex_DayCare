"""Bounded in-memory activity timeline for the demo (V1-DEMO-01).

Every event here is a fact the pipeline observed about itself: a track it confirmed, a track it
stopped seeing, a head count that changed, a camera that connected or went away. Nothing is
seeded, scheduled or replayed, so an empty room produces an empty timeline and the dashboard
says exactly that.

**The naming is the point.** These are camera-view facts, not building facts:

``PERSON_APPEARED_IN_VIEW`` — a confirmed track started. It does **not** mean somebody entered
the room; they may have stepped out from behind a cupboard, or the detector may finally have
picked up someone who was there all along.

``PERSON_NO_LONGER_VISIBLE`` — a track ended. It does **not** mean somebody left; occlusion,
turning away and walking out of frame all look identical from here.

Calling these ``TEACHER_ENTERED_CLASSROOM`` / ``TEACHER_EXITED_CLASSROOM`` would assert two
things this stage cannot support: that the appearance was a physical entry, and that the person
is a teacher. Entry and exit need a configured doorway or line-crossing semantic; identity needs
B1B and a completed B0 qualification. Neither exists, so neither is claimed.

History is capped, in memory, and session-scoped. There is no event table in the control plane,
and this is explicitly not a durable safety record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

MAX_EVENTS = 200


class DemoEventKind(StrEnum):
    TRACKING_STARTED = "TRACKING_STARTED"
    TRACKING_STOPPED = "TRACKING_STOPPED"
    CAMERA_CONNECTED = "CAMERA_CONNECTED"
    CAMERA_DISCONNECTED = "CAMERA_DISCONNECTED"
    PERSON_APPEARED_IN_VIEW = "PERSON_APPEARED_IN_VIEW"
    PERSON_NO_LONGER_VISIBLE = "PERSON_NO_LONGER_VISIBLE"
    OCCUPANCY_CHANGED = "OCCUPANCY_CHANGED"


@dataclass(frozen=True, slots=True)
class DemoEvent:
    sequence: int
    kind: DemoEventKind
    occurred_at: str
    session_ms: float
    # Bounded, non-identifying detail: a track id and counts. Never a name, never an image.
    track_id: int | None = None
    occupancy: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": str(self.kind),
            "occurred_at": self.occurred_at,
            "session_ms": round(self.session_ms, 1),
            "track_id": self.track_id,
            "occupancy": self.occupancy,
        }


class DemoTimeline:
    """A capped ring of events. Oldest are discarded; memory does not grow with session age."""

    def __init__(self, *, capacity: int = MAX_EVENTS) -> None:
        if capacity < 1:
            raise ValueError("timeline capacity must be positive")
        self._events: deque[DemoEvent] = deque(maxlen=capacity)
        self._sequence = 0
        self._occupancy = 0
        self.peak_occupancy = 0

    @property
    def occupancy(self) -> int:
        return self._occupancy

    @property
    def capacity(self) -> int:
        return self._events.maxlen or 0

    def record(
        self,
        kind: DemoEventKind,
        *,
        session_ms: float,
        track_id: int | None = None,
        occupancy: int | None = None,
    ) -> DemoEvent:
        self._sequence += 1
        event = DemoEvent(
            sequence=self._sequence,
            kind=kind,
            occurred_at=datetime.now(UTC).isoformat(),
            session_ms=session_ms,
            track_id=track_id,
            occupancy=occupancy,
        )
        self._events.append(event)
        return event

    def set_occupancy(self, value: int, *, session_ms: float) -> DemoEvent | None:
        """Record a head-count change, if it actually changed."""
        if value == self._occupancy:
            return None
        self._occupancy = value
        self.peak_occupancy = max(self.peak_occupancy, value)
        return self.record(DemoEventKind.OCCUPANCY_CHANGED, session_ms=session_ms, occupancy=value)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent first, bounded by ``limit``."""
        events = list(self._events)[-limit:]
        return [event.as_dict() for event in reversed(events)]

    def __len__(self) -> int:
        return len(self._events)
