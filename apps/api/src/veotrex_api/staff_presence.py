"""Ratio-eligible staff roster and authoritative staff check-in/out: the pure core (V1-04C).

Like :mod:`veotrex_api.classroom_ratio`, nothing here touches a database, a clock, a camera or a
network; every function takes ``now`` explicitly and returns a new immutable value.

**Three separate facts about an adult staff member.**

* *Identity* - the enrolled :class:`~veotrex_api.models.StaffProfile` (who they are).
* *Eligibility* - an operator placed them on a facility's roster and said whether they count
  toward the configured classroom ratio (:class:`EligibilityTerms`). "Qualified" in this domain
  means exactly that designation; VeoTrex verifies no licence and makes no legal claim.
* *Presence* - an operator checked them into a classroom, for a bounded lease
  (:class:`StaffPresenceEventRecord`).

Face recognition is none of the three. Nothing in this module accepts a recognition result, a
camera track or an occupancy count, and the only :class:`PresenceSource` it ever produces is
STAFF_ROSTER. A person being recognised on camera does not check them in.

**Current presence is derived, never stored.** A person's events form one stream ordered by a
per-person ``sequence``; the latest event alone decides where they are. CHECKED_IN and REFRESHED
open a lease that ends at ``valid_until``; CHECKED_OUT closes it. A lease that ran out without a
check-out is STALE: the person is not counted, and nothing older is reconsidered.

**Source precedence** (:func:`compose_presence`): children and visitors come only from the
latest MANUAL report; qualified staff come from the manual report in MANUAL_AGGREGATE mode and
from the roster in ROSTER_STAFF_PLUS_MANUAL_CHILDREN mode - never both, never added.
STAFF_RECOGNITION and anything vision-derived are never authoritative.
"""

from __future__ import annotations

import math
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
    PresenceAvailability,
    PresenceCount,
    PresenceResolution,
    PresenceRole,
    PresenceSnapshot,
    PresenceSource,
    RatioPolicyError,
    freshness,
    periods_overlap,
)

# A check-in describes a person who may leave without anyone noticing, so it is never trusted
# indefinitely. 15 minutes by default, renewable by an operator refresh; 1 minute to 4 hours.
# These bounds are mirrored by CHECKs in migration 0011.
STAFF_LEASE_MIN_SECONDS = 60
STAFF_LEASE_MAX_SECONDS = 4 * 60 * 60
STAFF_LEASE_DEFAULT_SECONDS = 15 * 60
# The roster count is recomputed at every evaluation; with nobody counted there is no lease to
# bound it, so a zero count is valid for the shortest lease.
EMPTY_ROSTER_VALIDITY_SECONDS = STAFF_LEASE_MIN_SECONDS


class StaffPresenceError(ValueError):
    """A roster operation was refused. The category names the rule, never the person."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _aware(value: datetime, category: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StaffPresenceError(category)


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso_utc(value: datetime | None) -> str | None:
    """Timestamps leave in UTC whatever the database session's timezone was."""
    return None if value is None else value.astimezone(UTC).isoformat()


def validate_lease(seconds: int) -> None:
    if not _strict_int(seconds) or not (
        STAFF_LEASE_MIN_SECONDS <= seconds <= STAFF_LEASE_MAX_SECONDS
    ):
        raise StaffPresenceError("invalid_lease_seconds")


# ----------------------------------------------------------------------------- source mode
class PresenceSourceMode(StrEnum):
    """Where a classroom's qualified-staff count comes from. Chosen explicitly per classroom."""

    # V1-04B: one manual report supplies children, qualified staff and visitors.
    MANUAL_AGGREGATE = "MANUAL_AGGREGATE"
    # V1-04C: staff from the check-in roster; children and visitors from the manual report.
    ROSTER_STAFF_PLUS_MANUAL_CHILDREN = "ROSTER_STAFF_PLUS_MANUAL_CHILDREN"


# The sources this stage lets decide each ratio slot. STAFF_RECOGNITION, ATTENDANCE and
# OTHER_APPROVED_SOURCE exist in the engine's vocabulary but are not connected; a count carrying
# them is refused here rather than silently used.
AUTHORITATIVE_SOURCES: dict[PresenceRole, frozenset[PresenceSource]] = {
    PresenceRole.CHILD: frozenset({PresenceSource.MANUAL}),
    PresenceRole.QUALIFIED_STAFF: frozenset({PresenceSource.MANUAL, PresenceSource.STAFF_ROSTER}),
    PresenceRole.VISITOR: frozenset({PresenceSource.MANUAL}),
}


def require_authoritative(count: PresenceCount | None) -> None:
    if count is None:
        return
    allowed = AUTHORITATIVE_SOURCES.get(count.role, frozenset())
    if count.source not in allowed:
        raise RatioPolicyError("source_not_authoritative")


# ----------------------------------------------------------------------------- eligibility
@dataclass(frozen=True, slots=True)
class EligibilityTerms:
    """One operator designation of one staff profile at one facility."""

    assignment_id: UUID
    staff_profile_id: UUID
    facility_id: UUID
    counts_toward_ratio: bool
    active: bool
    effective_from: datetime
    effective_until: datetime | None
    revision: int

    def __post_init__(self) -> None:
        _aware(self.effective_from, "effective_from_must_be_utc_aware")
        if self.effective_until is not None:
            _aware(self.effective_until, "effective_until_must_be_utc_aware")
            if self.effective_until <= self.effective_from:
                raise StaffPresenceError("effective_period_inverted")
        if not isinstance(self.counts_toward_ratio, bool):
            raise StaffPresenceError("invalid_counts_toward_ratio")
        if self.revision < 1:
            raise StaffPresenceError("invalid_revision")

    def applies_at(self, at: datetime) -> bool:
        _aware(at, "evaluation_time_must_be_utc_aware")
        if not self.active or at < self.effective_from:
            return False
        return self.effective_until is None or at < self.effective_until

    def overlaps(self, other: EligibilityTerms) -> bool:
        return periods_overlap(
            self.effective_from, self.effective_until, other.effective_from, other.effective_until
        )


def select_eligibility(
    assignments: Iterable[EligibilityTerms],
    staff_profile_id: UUID,
    facility_id: UUID,
    at: datetime,
) -> EligibilityTerms | None:
    """The designation in force for this person at this facility at ``at``.

    The database allows one ACTIVE row per (facility, staff), so there is normally at most one
    candidate. Should rows written outside the API ever produce two, the choice is still
    deterministic - latest start, then highest revision, then id - and, because a person who
    counts under one row and not under another is ambiguous, :func:`resolve_staff_count` does
    not count them (see ``ambiguous`` below).
    """
    applicable = [
        item
        for item in assignments
        if item.staff_profile_id == staff_profile_id
        and item.facility_id == facility_id
        and item.applies_at(at)
    ]
    if not applicable:
        return None
    applicable.sort(
        key=lambda item: (item.effective_from, item.revision, str(item.assignment_id)),
        reverse=True,
    )
    return applicable[0]


def eligibility_ambiguous(
    assignments: Iterable[EligibilityTerms],
    staff_profile_id: UUID,
    facility_id: UUID,
    at: datetime,
) -> bool:
    applicable = [
        item
        for item in assignments
        if item.staff_profile_id == staff_profile_id
        and item.facility_id == facility_id
        and item.applies_at(at)
    ]
    return len({item.counts_toward_ratio for item in applicable}) > 1


# -------------------------------------------------------------------------------- presence
class StaffPresenceEventType(StrEnum):
    CHECKED_IN = "CHECKED_IN"
    REFRESHED = "REFRESHED"
    CHECKED_OUT = "CHECKED_OUT"


OPEN_EVENTS = frozenset({StaffPresenceEventType.CHECKED_IN, StaffPresenceEventType.REFRESHED})


class StaffPresenceState(StrEnum):
    PRESENT = "PRESENT"  # an open lease that has not run out
    STALE = "STALE"  # the lease ran out and nobody checked the person out
    NOT_CHECKED_IN = "NOT_CHECKED_IN"  # never checked in, or checked out


@dataclass(frozen=True, slots=True)
class StaffPresenceEventRecord:
    """One stored event as the domain sees it. No image, face, embedding or track exists here."""

    event_id: UUID
    staff_profile_id: UUID
    facility_id: UUID
    classroom_id: UUID
    sequence: int
    event_type: StaffPresenceEventType
    occurred_at: datetime
    valid_until: datetime | None
    checked_in_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, StaffPresenceEventType):
            raise StaffPresenceError("invalid_event_type")
        if not _strict_int(self.sequence) or self.sequence < 1:
            raise StaffPresenceError("invalid_sequence")
        _aware(self.occurred_at, "presence_timestamp_must_be_utc_aware")
        _aware(self.checked_in_at, "presence_timestamp_must_be_utc_aware")
        if self.checked_in_at > self.occurred_at:
            raise StaffPresenceError("invalid_session_start")
        if self.event_type is StaffPresenceEventType.CHECKED_OUT:
            if self.valid_until is not None:
                raise StaffPresenceError("check_out_has_no_lease")
            return
        if self.valid_until is None:
            raise StaffPresenceError("open_event_needs_a_lease")
        _aware(self.valid_until, "presence_timestamp_must_be_utc_aware")
        seconds = (self.valid_until - self.occurred_at).total_seconds()
        if not STAFF_LEASE_MIN_SECONDS <= seconds <= STAFF_LEASE_MAX_SECONDS:
            raise StaffPresenceError("invalid_lease_seconds")

    @property
    def opens(self) -> bool:
        return self.event_type in OPEN_EVENTS


@dataclass(frozen=True, slots=True)
class CurrentStaffPresence:
    """Where one person is, derived from their latest event at ``evaluated_at``."""

    staff_profile_id: UUID
    state: StaffPresenceState
    # Set whenever the latest event is open (PRESENT or STALE); None once checked out.
    facility_id: UUID | None
    classroom_id: UUID | None
    checked_in_at: datetime | None
    last_event_at: datetime | None
    valid_until: datetime | None
    # The latest sequence seen (0 = no events); the next event is ``sequence + 1``.
    sequence: int
    evaluated_at: datetime

    @property
    def open(self) -> bool:
        return self.state is not StaffPresenceState.NOT_CHECKED_IN

    def in_classroom(self, classroom_id: UUID) -> bool:
        return self.open and self.classroom_id == classroom_id


def current_presence(
    events: Iterable[StaffPresenceEventRecord], staff_profile_id: UUID, now: datetime
) -> CurrentStaffPresence:
    """The person's latest event decides. Nothing older is ever reconsidered."""
    _aware(now, "evaluation_time_must_be_utc_aware")
    own = [event for event in events if event.staff_profile_id == staff_profile_id]
    if not own:
        return CurrentStaffPresence(
            staff_profile_id,
            StaffPresenceState.NOT_CHECKED_IN,
            None,
            None,
            None,
            None,
            None,
            0,
            now,
        )
    latest = max(own, key=lambda event: event.sequence)
    if not latest.opens:
        return CurrentStaffPresence(
            staff_profile_id,
            StaffPresenceState.NOT_CHECKED_IN,
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
    return CurrentStaffPresence(
        staff_profile_id,
        StaffPresenceState.PRESENT if fresh else StaffPresenceState.STALE,
        latest.facility_id,
        latest.classroom_id,
        latest.checked_in_at,
        latest.occurred_at,
        latest.valid_until,
        latest.sequence,
        now,
    )


# ---------------------------------------------------------------------- state transitions
class TransitionKind(StrEnum):
    CHECKED_IN = "CHECKED_IN"
    MOVED = "MOVED"  # an atomic check-out of the previous room and check-in to this one
    REFRESHED = "REFRESHED"
    CHECKED_OUT = "CHECKED_OUT"
    UNCHANGED = "UNCHANGED"  # an idempotent check-out of someone not in the room


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    """An event to append, in order. ``sequence`` continues the person's stream."""

    event_type: StaffPresenceEventType
    facility_id: UUID
    classroom_id: UUID
    sequence: int
    occurred_at: datetime
    valid_until: datetime | None
    checked_in_at: datetime


@dataclass(frozen=True, slots=True)
class PresenceTransition:
    kind: TransitionKind
    events: tuple[PlannedEvent, ...]
    previous: CurrentStaffPresence


def plan_check_in(
    current: CurrentStaffPresence,
    *,
    classroom_id: UUID,
    facility_id: UUID,
    now: datetime,
    lease_seconds: int,
    staff_active: bool,
    classroom_active: bool,
    eligibility: EligibilityTerms | None,
) -> PresenceTransition:
    """Check a person into a classroom. One deterministic outcome for every prior state:

    * not checked in, or stale in this room      -> CHECKED_IN here;
    * present here already                       -> refused ``staff_already_checked_in`` (use
      refresh to extend the lease, so a repeated click never silently resets it);
    * open (present or stale) in another room of the same facility -> MOVED: CHECKED_OUT there
      and CHECKED_IN here, appended together so no moment exists with the person in two rooms;
    * present in another facility                -> refused ``staff_checked_in_elsewhere``: this
      operator may not administer that facility, so it is checked out there first (or lapses);
    * stale in another facility                  -> CHECKED_IN here; the lapsed stay needs no
      write in a facility this operator may not administer, and the new latest event supersedes
      it.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    if not classroom_active:
        raise StaffPresenceError("classroom_inactive")
    if not staff_active:
        raise StaffPresenceError("staff_not_active")
    if (
        eligibility is None
        or eligibility.facility_id != facility_id
        or eligibility.staff_profile_id != current.staff_profile_id
        or not eligibility.applies_at(now)
    ):
        raise StaffPresenceError("staff_not_assigned_to_facility")
    validate_lease(lease_seconds)
    until = now + timedelta(seconds=lease_seconds)
    next_sequence = current.sequence + 1

    def check_in(sequence: int) -> PlannedEvent:
        return PlannedEvent(
            StaffPresenceEventType.CHECKED_IN, facility_id, classroom_id, sequence, now, until, now
        )

    if current.open and current.classroom_id == classroom_id:
        if current.state is StaffPresenceState.PRESENT:
            raise StaffPresenceError("staff_already_checked_in")
        return PresenceTransition(TransitionKind.CHECKED_IN, (check_in(next_sequence),), current)
    if current.open and current.facility_id == facility_id:
        assert current.classroom_id is not None and current.checked_in_at is not None
        leave = PlannedEvent(
            StaffPresenceEventType.CHECKED_OUT,
            facility_id,
            current.classroom_id,
            next_sequence,
            now,
            None,
            current.checked_in_at,
        )
        events = (leave, check_in(next_sequence + 1))
        return PresenceTransition(TransitionKind.MOVED, events, current)
    if current.state is StaffPresenceState.PRESENT:
        raise StaffPresenceError("staff_checked_in_elsewhere")
    return PresenceTransition(TransitionKind.CHECKED_IN, (check_in(next_sequence),), current)


def plan_check_out(
    current: CurrentStaffPresence, *, classroom_id: UUID, now: datetime
) -> PresenceTransition:
    """Check a person out of a classroom.

    Open here (present or lapsed) -> CHECKED_OUT, which also closes a lapsed stay on the record.
    Present in another room -> refused ``staff_in_another_classroom``: checking out of the wrong
    room must not end a real stay elsewhere. Anything else -> UNCHANGED, idempotently: nothing
    is appended and nothing is audited.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    if current.in_classroom(classroom_id):
        assert current.facility_id is not None and current.checked_in_at is not None
        leave = PlannedEvent(
            StaffPresenceEventType.CHECKED_OUT,
            current.facility_id,
            classroom_id,
            current.sequence + 1,
            now,
            None,
            current.checked_in_at,
        )
        return PresenceTransition(TransitionKind.CHECKED_OUT, (leave,), current)
    if current.state is StaffPresenceState.PRESENT:
        raise StaffPresenceError("staff_in_another_classroom")
    return PresenceTransition(TransitionKind.UNCHANGED, (), current)


def plan_refresh(
    current: CurrentStaffPresence,
    *,
    classroom_id: UUID,
    now: datetime,
    lease_seconds: int,
    staff_active: bool,
    classroom_active: bool,
    eligibility: EligibilityTerms | None,
) -> PresenceTransition:
    """Extend a current stay by a new bounded lease, measured from ``now``.

    Only a stay that is still PRESENT in this room can be refreshed. A lapsed one is refused
    ``staff_presence_expired``: the person may have left, so an operator checks them in again
    rather than reviving the old stay.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    if not classroom_active:
        raise StaffPresenceError("classroom_inactive")
    if not current.in_classroom(classroom_id):
        raise StaffPresenceError("staff_not_checked_in")
    if current.state is StaffPresenceState.STALE:
        raise StaffPresenceError("staff_presence_expired")
    if not staff_active:
        raise StaffPresenceError("staff_not_active")
    assert current.facility_id is not None and current.checked_in_at is not None
    if (
        eligibility is None
        or eligibility.facility_id != current.facility_id
        or not eligibility.applies_at(now)
    ):
        raise StaffPresenceError("staff_not_assigned_to_facility")
    validate_lease(lease_seconds)
    event = PlannedEvent(
        StaffPresenceEventType.REFRESHED,
        current.facility_id,
        classroom_id,
        current.sequence + 1,
        now,
        now + timedelta(seconds=lease_seconds),
        current.checked_in_at,
    )
    return PresenceTransition(TransitionKind.REFRESHED, (event,), current)


# ------------------------------------------------------------------------- staff count
@dataclass(frozen=True, slots=True)
class RosterMember:
    """A staff profile as the resolver needs it: identity and operator status, nothing else."""

    staff_profile_id: UUID
    active: bool


@dataclass(frozen=True, slots=True)
class StaffCountResolution:
    """The authoritative qualified-staff count for one classroom, with bounded provenance.

    Numbers only - no names. ``count`` is the people who are all of: an existing ACTIVE profile,
    PRESENT in this classroom on an unexpired lease, and designated at this classroom's facility
    as counting toward the configured ratio. Everyone else present is reported in a bucket that
    explains why they did not count.
    """

    classroom_id: UUID
    facility_id: UUID
    evaluated_at: datetime
    count: int
    present: int  # PRESENT here, whatever their eligibility
    present_ratio_ineligible: int  # PRESENT here, active, but not designated as counting
    present_inactive: int  # PRESENT here, but their profile is inactive or deleted
    present_ambiguous: int  # PRESENT here with conflicting designations; not counted
    stale: int  # lease in this room ran out without a check-out
    valid_until: datetime | None  # earliest lease among the people counted
    counted_staff_ids: tuple[UUID, ...]
    source: PresenceSource = PresenceSource.STAFF_ROSTER

    @property
    def freshness(self) -> Freshness:
        # A derived count is computed at evaluation time from unexpired leases only.
        return Freshness.FRESH

    def valid_for_seconds(self) -> int:
        if self.valid_until is None:
            return EMPTY_ROSTER_VALIDITY_SECONDS
        remaining = math.floor((self.valid_until - self.evaluated_at).total_seconds())
        return max(1, min(remaining, MAX_VALIDITY_SECONDS))

    def to_presence_count(self) -> PresenceCount:
        return PresenceCount(
            self.classroom_id,
            PresenceRole.QUALIFIED_STAFF,
            self.count,
            PresenceSource.STAFF_ROSTER,
            self.evaluated_at,
            self.valid_for_seconds(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "count": self.count,
            "present": self.present,
            "present_ratio_ineligible": self.present_ratio_ineligible,
            "present_inactive": self.present_inactive,
            "present_ambiguous": self.present_ambiguous,
            "stale": self.stale,
            "freshness": str(self.freshness),
            "valid_until": _iso_utc(self.valid_until),
            "evaluated_at": _iso_utc(self.evaluated_at),
        }


def resolve_staff_count(
    *,
    classroom_id: UUID,
    facility_id: UUID,
    now: datetime,
    members: Iterable[RosterMember],
    events: Iterable[StaffPresenceEventRecord],
    assignments: Iterable[EligibilityTerms],
) -> StaffCountResolution:
    """classroom + time + presence events + eligibility -> qualified staff count.

    Only people in ``members`` (existing profiles) are considered, so an event for a profile
    that does not exist counts nobody. Never counted: an inactive or deleted profile, a lapsed
    lease, a check-out, a stay in another room or facility, anyone not designated as counting
    toward the ratio at this facility today. There is no parameter through which a camera, a
    recognition match or an UNKNOWN person could add to the count.
    """
    _aware(now, "evaluation_time_must_be_utc_aware")
    event_list = list(events)
    assignment_list = list(assignments)
    seen: set[UUID] = set()
    counted: list[UUID] = []
    present = ineligible = inactive = ambiguous = stale = 0
    earliest: datetime | None = None
    for member in sorted(members, key=lambda item: str(item.staff_profile_id)):
        if member.staff_profile_id in seen:
            continue
        seen.add(member.staff_profile_id)
        current = current_presence(event_list, member.staff_profile_id, now)
        if not current.in_classroom(classroom_id) or current.facility_id != facility_id:
            continue
        if current.state is StaffPresenceState.STALE:
            stale += 1
            continue
        present += 1
        if not member.active:
            inactive += 1
            continue
        if eligibility_ambiguous(assignment_list, member.staff_profile_id, facility_id, now):
            ambiguous += 1
            continue
        terms = select_eligibility(assignment_list, member.staff_profile_id, facility_id, now)
        if terms is None or not terms.counts_toward_ratio:
            ineligible += 1
            continue
        counted.append(member.staff_profile_id)
        assert current.valid_until is not None
        if earliest is None or current.valid_until < earliest:
            earliest = current.valid_until
    return StaffCountResolution(
        classroom_id=classroom_id,
        facility_id=facility_id,
        evaluated_at=now,
        count=len(counted),
        present=present,
        present_ratio_ineligible=ineligible,
        present_inactive=inactive,
        present_ambiguous=ambiguous,
        stale=stale,
        valid_until=earliest,
        counted_staff_ids=tuple(counted),
    )


# ------------------------------------------------------------------ composite presence
@dataclass(frozen=True, slots=True)
class SlotProvenance:
    """Where one ratio input came from. The count is shown only while it is usable."""

    count: int | None
    source: PresenceSource | None
    freshness: Freshness
    valid_until: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "source": None if self.source is None else str(self.source),
            "freshness": str(self.freshness),
            "valid_until": _iso_utc(self.valid_until),
        }


@dataclass(frozen=True, slots=True)
class CompositePresence:
    mode: PresenceSourceMode
    snapshot: PresenceSnapshot | None
    children: SlotProvenance
    qualified_staff: SlotProvenance
    visitors: SlotProvenance
    roster: StaffCountResolution | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "children": self.children.as_dict(),
            "qualified_staff": self.qualified_staff.as_dict(),
            "visitors": self.visitors.as_dict(),
            "staff_roster": None if self.roster is None else self.roster.as_dict(),
        }


def _slot(value: PresenceCount | None, now: datetime) -> SlotProvenance:
    state = freshness(value, now)
    if value is None:
        return SlotProvenance(None, None, state, None)
    return SlotProvenance(
        value.count if state is Freshness.FRESH else None, value.source, state, value.expires_at
    )


def compose_presence(
    classroom_id: UUID,
    mode: PresenceSourceMode,
    manual: PresenceResolution,
    roster: StaffCountResolution | None,
    now: datetime,
) -> CompositePresence:
    """Build the one :class:`PresenceSnapshot` the ratio engine sees, slot by slot.

    MANUAL_AGGREGATE: exactly V1-04B - every slot from the latest manual report.
    ROSTER_STAFF_PLUS_MANUAL_CHILDREN: children and visitors from the latest manual report,
    qualified staff from ``roster`` alone. A staff number on the manual report is ignored, never
    added: roster staff + manual staff would count the same teacher twice.

    A revoked or absent manual report leaves children missing, so the ratio is
    INSUFFICIENT_DATA whatever the roster says; a stale one passes through as stale, as before.
    """
    if not isinstance(mode, PresenceSourceMode):
        raise RatioPolicyError("invalid_presence_source_mode")
    manual_snapshot = manual.snapshot
    if manual.availability is PresenceAvailability.PRESENCE_REVOKED:
        manual_snapshot = None
    children = None if manual_snapshot is None else manual_snapshot.children
    visitors = None if manual_snapshot is None else manual_snapshot.visitors
    if mode is PresenceSourceMode.MANUAL_AGGREGATE:
        staff = None if manual_snapshot is None else manual_snapshot.qualified_staff
    else:
        if roster is None or roster.classroom_id != classroom_id:
            raise RatioPolicyError("staff_roster_required")
        staff = roster.to_presence_count()
    for value in (children, staff, visitors):
        require_authoritative(value)
    snapshot = (
        None
        if children is None and staff is None and visitors is None
        else PresenceSnapshot(
            classroom_id, children=children, qualified_staff=staff, visitors=visitors
        )
    )
    return CompositePresence(
        mode=mode,
        snapshot=snapshot,
        children=_slot(children, now),
        qualified_staff=_slot(staff, now),
        visitors=_slot(visitors, now),
        roster=roster if mode is PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN else None,
    )
