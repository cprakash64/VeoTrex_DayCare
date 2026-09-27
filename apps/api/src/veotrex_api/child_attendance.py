"""Child roster and authoritative attendance check-in/out: the pure core (V1-04D).

Like :mod:`veotrex_api.classroom_ratio` and :mod:`veotrex_api.staff_presence`, nothing here
touches a database, a clock, a camera or a network; every function takes ``now`` explicitly.

**A child here is a roster entry, not a biometric identity.** A child profile is an operator's
record that a child attends a facility - an opaque id, a display name for the operator's own
screens, a status, and an optional external reference for a future attendance-system connector.
There is no photo, face, embedding, date of birth, address, medical, guardian or camera field
anywhere, and nothing in this module accepts an image, a track, an occupancy count or a
recognition result. A camera never checks a child in, and an UNKNOWN person is never a child.

**Attendance is an event stream.** An operator checks a child into a classroom (CHECKED_IN),
extends the stay (REFRESHED) or checks them out (CHECKED_OUT). A child's latest event, ordered
by a per-child ``sequence``, alone decides where they are; there is no mutable "is present"
flag. Every open event carries a bounded lease, so a missed check-out lapses on its own and is
never carried into the next day.

**The count is aggregate.** :func:`resolve_child_count` returns a number with bounded
provenance - never names - and the ratio engine receives only that number as an ATTENDANCE
:class:`~veotrex_api.classroom_ratio.PresenceCount`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from veotrex_api.classroom_ratio import (
    MAX_FUTURE_SKEW,
    MAX_VALIDITY_SECONDS,
    Freshness,
    PresenceCount,
    PresenceRole,
    PresenceSource,
)

# A daycare session can legitimately last a working day, so the lease is long - but never longer
# than the ratio engine's own 12 h outer bound on any count (``MAX_VALIDITY_SECONDS``): a
# forgotten check-out lapses within 12 hours and is never carried into the next day's session.
# 30 minutes to 12 hours, 12 hours by default, renewable by an operator refresh while fresh.
# Mirrored by CHECKs in migration 0012.
ATTENDANCE_LEASE_MIN_SECONDS = 30 * 60
ATTENDANCE_LEASE_MAX_SECONDS = 12 * 60 * 60
ATTENDANCE_LEASE_DEFAULT_SECONDS = 12 * 60 * 60
# The count is recomputed at every evaluation; with nobody counted there is no lease to bound it.
EMPTY_ATTENDANCE_VALIDITY_SECONDS = 60

# Roster text. The display name is ordinary roster PII for an operator's own screens: bounded,
# NFC-normalised, whitespace-collapsed, no control or invisible formatting characters, no markup
# delimiters. The external reference is an identifier for a future attendance-system connector,
# deliberately restricted to identifier characters so it cannot hold a sentence or a name.
DISPLAY_NAME_MAX = 120
EXTERNAL_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class ChildAttendanceError(ValueError):
    """An attendance or roster operation was refused. The category names the rule, never the
    child."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _aware(value: datetime, category: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChildAttendanceError(category)


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def clean_display_name(value: str) -> str:
    """Normalise and validate a child's display name, or refuse ``invalid_display_name``."""
    if not isinstance(value, str):
        raise ChildAttendanceError("invalid_display_name")
    normalised = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in normalised.strip()):
        # Tabs and newlines inside the name are control characters too: refused, not collapsed.
        raise ChildAttendanceError("invalid_display_name")
    name = " ".join(normalised.split())
    if not name or len(name) > DISPLAY_NAME_MAX or "<" in name or ">" in name:
        raise ChildAttendanceError("invalid_display_name")
    return name


def clean_external_reference(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChildAttendanceError("invalid_external_reference")
    cleaned = value.strip()
    if not cleaned:
        return None
    if not EXTERNAL_REFERENCE_PATTERN.match(cleaned):
        raise ChildAttendanceError("invalid_external_reference")
    return cleaned


def validate_attendance_lease(seconds: int) -> None:
    if not _strict_int(seconds) or not (
        ATTENDANCE_LEASE_MIN_SECONDS <= seconds <= ATTENDANCE_LEASE_MAX_SECONDS
    ):
        raise ChildAttendanceError("invalid_lease_seconds")


def _iso_utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------------- roster
class ChildStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"  # temporarily not attending; can be reactivated
    ARCHIVED = "ARCHIVED"  # left the facility; terminal, kept for history


def status_transition(current: ChildStatus, target: ChildStatus) -> bool:
    """Whether a lifecycle change is permitted; False means "already there" (idempotent).

    ACTIVE <-> INACTIVE freely; either may be ARCHIVED; ARCHIVED is terminal.
    """
    if current is target:
        return False
    if current is ChildStatus.ARCHIVED:
        raise ChildAttendanceError("child_archived")
    return True


# ----------------------------------------------------------------------------- attendance
class AttendanceEventType(StrEnum):
    CHECKED_IN = "CHECKED_IN"
    REFRESHED = "REFRESHED"
    CHECKED_OUT = "CHECKED_OUT"


OPEN_EVENTS = frozenset({AttendanceEventType.CHECKED_IN, AttendanceEventType.REFRESHED})


class AttendanceState(StrEnum):
    PRESENT = "PRESENT"  # an open lease that has not run out
    STALE = "STALE"  # the lease ran out and nobody checked the child out
    NOT_CHECKED_IN = "NOT_CHECKED_IN"  # never checked in, or checked out


@dataclass(frozen=True, slots=True)
class AttendanceEventRecord:
    """One stored attendance event as the domain sees it. Ids and times only."""

    event_id: UUID
    child_profile_id: UUID
    facility_id: UUID
    classroom_id: UUID
    sequence: int
    event_type: AttendanceEventType
    occurred_at: datetime
    valid_until: datetime | None
    checked_in_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, AttendanceEventType):
            raise ChildAttendanceError("invalid_event_type")
        if not _strict_int(self.sequence) or self.sequence < 1:
            raise ChildAttendanceError("invalid_sequence")
        _aware(self.occurred_at, "attendance_timestamp_must_be_utc_aware")
        _aware(self.checked_in_at, "attendance_timestamp_must_be_utc_aware")
        if self.checked_in_at > self.occurred_at:
            raise ChildAttendanceError("invalid_session_start")
        if self.event_type is AttendanceEventType.CHECKED_OUT:
            if self.valid_until is not None:
                raise ChildAttendanceError("check_out_has_no_lease")
            return
        if self.valid_until is None:
            raise ChildAttendanceError("open_event_needs_a_lease")
        _aware(self.valid_until, "attendance_timestamp_must_be_utc_aware")
        seconds = (self.valid_until - self.occurred_at).total_seconds()
        if not ATTENDANCE_LEASE_MIN_SECONDS <= seconds <= ATTENDANCE_LEASE_MAX_SECONDS:
            raise ChildAttendanceError("invalid_lease_seconds")

    @property
    def opens(self) -> bool:
        return self.event_type in OPEN_EVENTS


@dataclass(frozen=True, slots=True)
class CurrentAttendance:
    """Where one child is, derived from their latest event at ``evaluated_at``."""

    child_profile_id: UUID
    state: AttendanceState
    facility_id: UUID | None
    classroom_id: UUID | None
    checked_in_at: datetime | None
    last_event_at: datetime | None
    valid_until: datetime | None
    sequence: int  # latest sequence seen; 0 = no events
    evaluated_at: datetime

    @property
    def open(self) -> bool:
        return self.state is not AttendanceState.NOT_CHECKED_IN

    def in_classroom(self, classroom_id: UUID) -> bool:
        return self.open and self.classroom_id == classroom_id


def current_attendance(
    events: Iterable[AttendanceEventRecord], child_profile_id: UUID, now: datetime
) -> CurrentAttendance:
    """The child's latest event decides. Nothing older is ever reconsidered, and another
    child's events are ignored entirely."""
    _aware(now, "evaluation_time_must_be_utc_aware")
    own = [event for event in events if event.child_profile_id == child_profile_id]
    if not own:
        return CurrentAttendance(
            child_profile_id, AttendanceState.NOT_CHECKED_IN, None, None, None, None, None, 0, now
        )
    latest = max(own, key=lambda event: event.sequence)
    if not latest.opens:
        return CurrentAttendance(
            child_profile_id,
            AttendanceState.NOT_CHECKED_IN,
            None,
            None,
            None,
            latest.occurred_at,
            None,
            latest.sequence,
            now,
        )
    assert latest.valid_until is not None
    fresh = latest.occurred_at - now <= MAX_FUTURE_SKEW and now < latest.valid_until
    return CurrentAttendance(
        child_profile_id,
        AttendanceState.PRESENT if fresh else AttendanceState.STALE,
        latest.facility_id,
        latest.classroom_id,
        latest.checked_in_at,
        latest.occurred_at,
        latest.valid_until,
        latest.sequence,
        now,
    )


# ---------------------------------------------------------------------- state transitions
class AttendanceTransitionKind(StrEnum):
    CHECKED_IN = "CHECKED_IN"
    MOVED = "MOVED"  # atomic check-out of the previous classroom and check-in to this one
    REFRESHED = "REFRESHED"
    CHECKED_OUT = "CHECKED_OUT"
    UNCHANGED = "UNCHANGED"  # an idempotent check-out of a child not in the room


@dataclass(frozen=True, slots=True)
class PlannedAttendanceEvent:
    event_type: AttendanceEventType
    facility_id: UUID
    classroom_id: UUID
    sequence: int
    occurred_at: datetime
    valid_until: datetime | None
    checked_in_at: datetime


@dataclass(frozen=True, slots=True)
class AttendanceTransition:
    kind: AttendanceTransitionKind
    events: tuple[PlannedAttendanceEvent, ...]
    previous: CurrentAttendance


def _require_checkable(status: ChildStatus, classroom_active: bool) -> None:
    if not classroom_active:
        raise ChildAttendanceError("classroom_inactive")
    if status is ChildStatus.ARCHIVED:
        raise ChildAttendanceError("child_archived")
    if status is not ChildStatus.ACTIVE:
        raise ChildAttendanceError("child_not_active")


def plan_check_in(
    current: CurrentAttendance,
    *,
    classroom_id: UUID,
    facility_id: UUID,
    child_facility_id: UUID,
    now: datetime,
    lease_seconds: int,
    child_status: ChildStatus,
    classroom_active: bool,
) -> AttendanceTransition:
    """Check a child into a classroom of their own facility. One outcome per prior state:

    * not checked in, or stale in this room      -> CHECKED_IN here;
    * present here already                       -> refused ``child_already_checked_in`` (use
      refresh, so a repeated click never silently resets the lease);
    * open (present or stale) in another room    -> MOVED: CHECKED_OUT there and CHECKED_IN
      here, appended together, so there is no moment with the child in two rooms.

    A child belongs to exactly one facility, so every room they can be in is in it.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    if child_facility_id != facility_id:
        raise ChildAttendanceError("child_facility_mismatch")
    _require_checkable(child_status, classroom_active)
    validate_attendance_lease(lease_seconds)
    until = now + timedelta(seconds=lease_seconds)
    next_sequence = current.sequence + 1

    def check_in(sequence: int) -> PlannedAttendanceEvent:
        return PlannedAttendanceEvent(
            AttendanceEventType.CHECKED_IN, facility_id, classroom_id, sequence, now, until, now
        )

    if current.open and current.classroom_id == classroom_id:
        if current.state is AttendanceState.PRESENT:
            raise ChildAttendanceError("child_already_checked_in")
        return AttendanceTransition(
            AttendanceTransitionKind.CHECKED_IN, (check_in(next_sequence),), current
        )
    if current.open:
        assert current.classroom_id is not None and current.checked_in_at is not None
        if current.facility_id != facility_id:
            raise ChildAttendanceError("child_facility_mismatch")
        leave = PlannedAttendanceEvent(
            AttendanceEventType.CHECKED_OUT,
            facility_id,
            current.classroom_id,
            next_sequence,
            now,
            None,
            current.checked_in_at,
        )
        events = (leave, check_in(next_sequence + 1))
        return AttendanceTransition(AttendanceTransitionKind.MOVED, events, current)
    return AttendanceTransition(
        AttendanceTransitionKind.CHECKED_IN, (check_in(next_sequence),), current
    )


def plan_check_out(
    current: CurrentAttendance, *, classroom_id: UUID, now: datetime
) -> AttendanceTransition:
    """Open here (present or lapsed) -> CHECKED_OUT. Present in another room -> refused
    ``child_in_another_classroom``. Otherwise UNCHANGED: idempotent, nothing appended."""
    _aware(now, "evaluation_time_must_be_utc_aware")
    if current.in_classroom(classroom_id):
        assert current.facility_id is not None and current.checked_in_at is not None
        leave = PlannedAttendanceEvent(
            AttendanceEventType.CHECKED_OUT,
            current.facility_id,
            classroom_id,
            current.sequence + 1,
            now,
            None,
            current.checked_in_at,
        )
        return AttendanceTransition(AttendanceTransitionKind.CHECKED_OUT, (leave,), current)
    if current.state is AttendanceState.PRESENT:
        raise ChildAttendanceError("child_in_another_classroom")
    return AttendanceTransition(AttendanceTransitionKind.UNCHANGED, (), current)


def plan_refresh(
    current: CurrentAttendance,
    *,
    classroom_id: UUID,
    now: datetime,
    lease_seconds: int,
    child_status: ChildStatus,
    classroom_active: bool,
) -> AttendanceTransition:
    """Extend a PRESENT stay in this room by a new bounded lease from ``now``. A lapsed stay is
    refused ``attendance_expired``: the child may have gone home, so an operator checks them in
    again rather than reviving yesterday's session."""
    _aware(now, "evaluation_time_must_be_utc_aware")
    if not classroom_active:
        raise ChildAttendanceError("classroom_inactive")
    if not current.in_classroom(classroom_id):
        raise ChildAttendanceError("child_not_checked_in")
    if current.state is AttendanceState.STALE:
        raise ChildAttendanceError("attendance_expired")
    _require_checkable(child_status, classroom_active)
    validate_attendance_lease(lease_seconds)
    assert current.facility_id is not None and current.checked_in_at is not None
    event = PlannedAttendanceEvent(
        AttendanceEventType.REFRESHED,
        current.facility_id,
        classroom_id,
        current.sequence + 1,
        now,
        now + timedelta(seconds=lease_seconds),
        current.checked_in_at,
    )
    return AttendanceTransition(AttendanceTransitionKind.REFRESHED, (event,), current)


# ------------------------------------------------------------------------------- count
@dataclass(frozen=True, slots=True)
class ChildRosterMember:
    """A child profile as the resolver needs it: an opaque id, its facility and its status.
    No name ever reaches the resolver."""

    child_profile_id: UUID
    facility_id: UUID
    status: ChildStatus


@dataclass(frozen=True, slots=True)
class ChildCountResolution:
    """The authoritative ATTENDANCE child count for one classroom, with bounded provenance."""

    classroom_id: UUID
    facility_id: UUID
    evaluated_at: datetime
    count: int
    present: int  # PRESENT here, whatever their profile status
    present_inactive: int  # PRESENT here, but the profile is inactive or archived
    stale: int  # lease in this room ran out without a check-out
    valid_until: datetime | None  # earliest lease among the children counted
    source: PresenceSource = PresenceSource.ATTENDANCE

    @property
    def freshness(self) -> Freshness:
        # Derived at evaluation time from unexpired leases only.
        return Freshness.FRESH

    def valid_for_seconds(self) -> int:
        if self.valid_until is None:
            return EMPTY_ATTENDANCE_VALIDITY_SECONDS
        remaining = math.floor((self.valid_until - self.evaluated_at).total_seconds())
        return max(1, min(remaining, MAX_VALIDITY_SECONDS))

    def to_presence_count(self) -> PresenceCount:
        return PresenceCount(
            self.classroom_id,
            PresenceRole.CHILD,
            self.count,
            PresenceSource.ATTENDANCE,
            self.evaluated_at,
            self.valid_for_seconds(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "count": self.count,
            "present": self.present,
            "present_inactive": self.present_inactive,
            "stale": self.stale,
            "freshness": str(self.freshness),
            "valid_until": _iso_utc(self.valid_until),
            "evaluated_at": _iso_utc(self.evaluated_at),
        }


def resolve_child_count(
    *,
    classroom_id: UUID,
    facility_id: UUID,
    now: datetime,
    members: Iterable[ChildRosterMember],
    events: Iterable[AttendanceEventRecord],
) -> ChildCountResolution:
    """classroom + time + child profiles + attendance events -> child count.

    A child counts only when their profile exists (is in ``members``), belongs to this
    classroom's facility, is ACTIVE, and their latest event is an unexpired check-in to this
    classroom. There is no parameter through which a camera, a person track, a recognition
    result or an UNKNOWN person could add a child.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    event_list = list(events)
    seen: set[UUID] = set()
    count = present = inactive = stale = 0
    earliest: datetime | None = None
    for member in sorted(members, key=lambda item: str(item.child_profile_id)):
        if member.child_profile_id in seen or member.facility_id != facility_id:
            continue
        seen.add(member.child_profile_id)
        current = current_attendance(event_list, member.child_profile_id, now)
        if not current.in_classroom(classroom_id) or current.facility_id != facility_id:
            continue
        if current.state is AttendanceState.STALE:
            stale += 1
            continue
        present += 1
        if member.status is not ChildStatus.ACTIVE:
            inactive += 1
            continue
        count += 1
        assert current.valid_until is not None
        if earliest is None or current.valid_until < earliest:
            earliest = current.valid_until
    return ChildCountResolution(
        classroom_id=classroom_id,
        facility_id=facility_id,
        evaluated_at=now,
        count=count,
        present=present,
        present_inactive=inactive,
        stale=stale,
        valid_until=earliest,
    )
