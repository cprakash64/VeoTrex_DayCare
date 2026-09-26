"""Configured classroom ratio policy: the pure decision core (V1-04A).

Nothing here touches a database, a clock, a camera or a network. Every function takes its inputs
explicitly - including ``now`` - and returns a new immutable value, so the same inputs always
give the same answer and a test can pin every branch.

**What the camera can and cannot tell us.** The person detector produces PERSON tracks. It does
not produce CHILD, TEACHER, PARENT or VISITOR, and nothing in this module lets it pretend to.
Role counts come only from :class:`PresenceCount`, whose :class:`PresenceSource` enumerates
approved role sources (manual entry, attendance, staff roster, staff recognition, another
explicitly approved source). There is no vision source. A camera's head count is a different
type, :class:`VisionObservation`, which the ratio engine does not accept at all: it is used only
by :func:`reconcile_vision`, a diagnostic that compares the authoritative total with what the
camera saw and never rewrites either.

That separation is the safety property. ``children = observed_people - recognised_staff`` would
turn every unidentified adult - a parent at pick-up, a maintenance visitor, a teacher the
recogniser missed - into a child, and every child the camera missed into slack in the ratio.
Neither error is acceptable when the output is "is this room adequately staffed?".

**UNKNOWN** is a role a count may carry, and it is inert: an UNKNOWN count is never a child
and never qualified staff, so it can never satisfy or violate a ratio.

**Stale is not safe.** A count carries its source timestamp and how long it stays valid. A
missing or stale child or staff count makes the ratio INSUFFICIENT_DATA - never the last good
answer and never the last violation.

**Configured, not certified.** A :class:`RatioPolicyTerms` is what an operator entered for a
classroom. Nothing here claims it matches any jurisdiction's law; the states are named
"within configured policy" / "over configured ratio" for that reason.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

# Presence counts are "how many people of this role are in the room right now". Bounded so a
# typo cannot produce a four-digit room, and validity is bounded so a count cannot be entered
# once and trusted all day.
MAX_PRESENCE_COUNT = 500
MIN_VALIDITY_SECONDS = 1
MAX_VALIDITY_SECONDS = 12 * 60 * 60
# A source timestamp further in the future than this is treated as untrustworthy (clock skew
# or a bad integration), and so as stale rather than fresh.
MAX_FUTURE_SKEW = timedelta(seconds=120)

# Policy numbers are operator-entered integers; these bounds reject nonsense, not law.
MAX_CHILDREN_PER_STAFF_LIMIT = 100
MAX_MINIMUM_STAFF = 50
MAX_GROUP_SIZE_LIMIT = 500


class RatioPolicyError(ValueError):
    """A policy value was rejected. The category names the rule, never the tenant or room."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


# ----------------------------------------------------------------------------------- roles
class PresenceRole(StrEnum):
    QUALIFIED_STAFF = "QUALIFIED_STAFF"
    CHILD = "CHILD"
    VISITOR = "VISITOR"
    # Present, role not established by any approved source. Never a child, never staff.
    UNKNOWN = "UNKNOWN"


class PresenceSource(StrEnum):
    """Approved sources of *role* evidence. Deliberately contains no vision/detector member."""

    MANUAL = "MANUAL"
    ATTENDANCE = "ATTENDANCE"
    STAFF_ROSTER = "STAFF_ROSTER"
    STAFF_RECOGNITION = "STAFF_RECOGNITION"
    OTHER_APPROVED_SOURCE = "OTHER_APPROVED_SOURCE"


# Which sources may assert which roles. Staff recognition identifies enrolled adults only, so
# it may assert QUALIFIED_STAFF and nothing else; it can never declare a child. A roster is
# about staff; attendance is about children and visitors. Manual entry and an explicitly
# approved integration may assert any role, including UNKNOWN.
SOURCE_ROLES: dict[PresenceSource, frozenset[PresenceRole]] = {
    PresenceSource.MANUAL: frozenset(PresenceRole),
    PresenceSource.ATTENDANCE: frozenset(
        {PresenceRole.CHILD, PresenceRole.VISITOR, PresenceRole.UNKNOWN}
    ),
    PresenceSource.STAFF_ROSTER: frozenset({PresenceRole.QUALIFIED_STAFF}),
    PresenceSource.STAFF_RECOGNITION: frozenset({PresenceRole.QUALIFIED_STAFF}),
    PresenceSource.OTHER_APPROVED_SOURCE: frozenset(PresenceRole),
}


def _require_aware(value: datetime, category: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RatioPolicyError(category)


@dataclass(frozen=True, slots=True)
class PresenceCount:
    """One authoritative count of one role in one classroom, with where it came from and until
    when it may be relied on. No names, no identities - a number and its provenance."""

    classroom_id: UUID
    role: PresenceRole
    count: int
    source: PresenceSource
    observed_at: datetime  # UTC, from the source
    valid_for_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, PresenceRole):
            raise RatioPolicyError("invalid_presence_role")
        if not isinstance(self.source, PresenceSource):
            # A vision observation, a string, anything not in the approved list.
            raise RatioPolicyError("unapproved_presence_source")
        if self.role not in SOURCE_ROLES[self.source]:
            raise RatioPolicyError("source_cannot_assert_role")
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise RatioPolicyError("invalid_presence_count")
        if not 0 <= self.count <= MAX_PRESENCE_COUNT:
            raise RatioPolicyError("invalid_presence_count")
        if (
            isinstance(self.valid_for_seconds, bool)
            or not isinstance(self.valid_for_seconds, int)
            or not MIN_VALIDITY_SECONDS <= self.valid_for_seconds <= MAX_VALIDITY_SECONDS
        ):
            raise RatioPolicyError("invalid_presence_validity")
        _require_aware(self.observed_at, "presence_timestamp_must_be_utc_aware")

    @property
    def expires_at(self) -> datetime:
        return self.observed_at + timedelta(seconds=self.valid_for_seconds)


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


def freshness(value: PresenceCount | None, now: datetime) -> Freshness:
    _require_aware(now, "evaluation_time_must_be_utc_aware")
    if value is None:
        return Freshness.MISSING
    if value.observed_at - now > MAX_FUTURE_SKEW:
        return Freshness.STALE
    return Freshness.FRESH if now < value.expires_at else Freshness.STALE


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    """The latest authoritative count per role for one classroom. Every field may be absent."""

    classroom_id: UUID
    children: PresenceCount | None = None
    qualified_staff: PresenceCount | None = None
    visitors: PresenceCount | None = None
    unknown: PresenceCount | None = None

    def __post_init__(self) -> None:
        expected = {
            "children": PresenceRole.CHILD,
            "qualified_staff": PresenceRole.QUALIFIED_STAFF,
            "visitors": PresenceRole.VISITOR,
            "unknown": PresenceRole.UNKNOWN,
        }
        for name, role in expected.items():
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, PresenceCount):
                raise RatioPolicyError("presence_must_come_from_an_approved_source")
            if value.role is not role:
                # A slot accepts exactly its own role: an UNKNOWN or VISITOR count can never be
                # placed where a child or staff count belongs.
                raise RatioPolicyError("presence_role_mismatch")
            if value.classroom_id != self.classroom_id:
                raise RatioPolicyError("presence_classroom_mismatch")

    @classmethod
    def latest(cls, classroom_id: UUID, counts: Iterable[PresenceCount]) -> PresenceSnapshot:
        """The most recent count per role for this classroom; other classrooms are ignored."""
        best: dict[PresenceRole, PresenceCount] = {}
        for value in counts:
            if value.classroom_id != classroom_id:
                continue
            current = best.get(value.role)
            if current is None or value.observed_at > current.observed_at:
                best[value.role] = value
        return cls(
            classroom_id,
            children=best.get(PresenceRole.CHILD),
            qualified_staff=best.get(PresenceRole.QUALIFIED_STAFF),
            visitors=best.get(PresenceRole.VISITOR),
            unknown=best.get(PresenceRole.UNKNOWN),
        )


@dataclass(frozen=True, slots=True)
class VisionObservation:
    """What a camera counted: people, with no role. Diagnostic input only.

    It is not a :class:`PresenceCount` and cannot be converted into one; the ratio engine does
    not accept it. ``observed_people`` is the edge's validated occupancy for the classroom.
    """

    classroom_id: UUID
    observed_people: int
    observed_at: datetime
    valid_for_seconds: int = 30

    def __post_init__(self) -> None:
        if isinstance(self.observed_people, bool) or not isinstance(self.observed_people, int):
            raise RatioPolicyError("invalid_vision_count")
        if not 0 <= self.observed_people <= MAX_PRESENCE_COUNT:
            raise RatioPolicyError("invalid_vision_count")
        if not MIN_VALIDITY_SECONDS <= self.valid_for_seconds <= MAX_VALIDITY_SECONDS:
            raise RatioPolicyError("invalid_vision_validity")
        _require_aware(self.observed_at, "vision_timestamp_must_be_utc_aware")


# ---------------------------------------------------------------------------------- policy
@dataclass(frozen=True, slots=True)
class RatioPolicyTerms:
    """An operator-configured classroom policy: the numbers and when they apply."""

    policy_id: UUID
    classroom_id: UUID
    label: str
    max_children_per_staff: int
    minimum_staff: int
    maximum_group_size: int | None
    effective_from: datetime
    effective_until: datetime | None
    active: bool
    revision: int
    age_band_label: str | None = None

    def __post_init__(self) -> None:
        validate_policy_numbers(
            self.max_children_per_staff, self.minimum_staff, self.maximum_group_size
        )
        validate_effective_period(self.effective_from, self.effective_until)
        if self.revision < 1:
            raise RatioPolicyError("invalid_revision")

    def applies_at(self, at: datetime) -> bool:
        _require_aware(at, "evaluation_time_must_be_utc_aware")
        if not self.active or at < self.effective_from:
            return False
        return self.effective_until is None or at < self.effective_until

    def overlaps(self, other: RatioPolicyTerms) -> bool:
        return periods_overlap(
            self.effective_from, self.effective_until, other.effective_from, other.effective_until
        )


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_policy_numbers(
    max_children_per_staff: int, minimum_staff: int, maximum_group_size: int | None
) -> None:
    if not _strict_int(max_children_per_staff) or not (
        0 < max_children_per_staff <= MAX_CHILDREN_PER_STAFF_LIMIT
    ):
        raise RatioPolicyError("invalid_max_children_per_staff")
    if not _strict_int(minimum_staff) or not 0 <= minimum_staff <= MAX_MINIMUM_STAFF:
        raise RatioPolicyError("invalid_minimum_staff")
    if maximum_group_size is not None and (
        not _strict_int(maximum_group_size) or not 0 < maximum_group_size <= MAX_GROUP_SIZE_LIMIT
    ):
        raise RatioPolicyError("invalid_maximum_group_size")


def validate_effective_period(effective_from: datetime, effective_until: datetime | None) -> None:
    _require_aware(effective_from, "effective_from_must_be_utc_aware")
    if effective_until is not None:
        _require_aware(effective_until, "effective_until_must_be_utc_aware")
        if effective_until <= effective_from:
            raise RatioPolicyError("effective_period_inverted")


def periods_overlap(
    a_from: datetime, a_until: datetime | None, b_from: datetime, b_until: datetime | None
) -> bool:
    """Half-open ``[from, until)`` periods; ``None`` means open-ended."""
    a_end_after_b_start = a_until is None or b_from < a_until
    b_end_after_a_start = b_until is None or a_from < b_until
    return a_end_after_b_start and b_end_after_a_start


@dataclass(frozen=True, slots=True)
class PolicySelection:
    policy: RatioPolicyTerms | None
    # More than one active policy applied at once. Writes refuse to create this; if it is ever
    # observed (e.g. rows written outside the API) the choice below is deterministic and the
    # ambiguity is reported rather than hidden.
    ambiguous: bool = False


def select_policy(policies: Sequence[RatioPolicyTerms], at: datetime) -> PolicySelection:
    """The policy in force at ``at``: active, started, not yet ended.

    Deterministic on ambiguity: the most recently started period wins, then the highest
    revision, then the policy id - never insertion order.
    """
    applicable = [policy for policy in policies if policy.applies_at(at)]
    if not applicable:
        return PolicySelection(None)
    applicable.sort(key=lambda p: (p.effective_from, p.revision, str(p.policy_id)), reverse=True)
    return PolicySelection(applicable[0], ambiguous=len(applicable) > 1)


def find_overlap(
    candidate: RatioPolicyTerms, existing: Iterable[RatioPolicyTerms]
) -> RatioPolicyTerms | None:
    """An active policy of the same classroom whose period overlaps ``candidate``'s."""
    if not candidate.active:
        return None
    for other in existing:
        if other.policy_id == candidate.policy_id or other.classroom_id != candidate.classroom_id:
            continue
        if other.active and candidate.overlaps(other):
            return other
    return None


# ------------------------------------------------------------------------------ evaluation
class RatioState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_CHILDREN_PRESENT = "NO_CHILDREN_PRESENT"
    WITHIN_CONFIGURED_POLICY = "WITHIN_CONFIGURED_POLICY"
    OVER_CONFIGURED_RATIO = "OVER_CONFIGURED_RATIO"
    OVER_CONFIGURED_GROUP_SIZE = "OVER_CONFIGURED_GROUP_SIZE"


class Explanation(StrEnum):
    POLICY_NOT_CONFIGURED = "POLICY_NOT_CONFIGURED"
    CLASSROOM_INACTIVE = "CLASSROOM_INACTIVE"
    POLICY_SELECTION_AMBIGUOUS = "POLICY_SELECTION_AMBIGUOUS"
    CHILD_COUNT_MISSING = "CHILD_COUNT_MISSING"
    CHILD_COUNT_STALE = "CHILD_COUNT_STALE"
    STAFF_COUNT_MISSING = "STAFF_COUNT_MISSING"
    STAFF_COUNT_STALE = "STAFF_COUNT_STALE"
    NO_CHILDREN_PRESENT = "NO_CHILDREN_PRESENT"
    NO_QUALIFIED_STAFF_PRESENT = "NO_QUALIFIED_STAFF_PRESENT"
    STAFF_BELOW_REQUIRED = "STAFF_BELOW_REQUIRED"
    GROUP_SIZE_EXCEEDED = "GROUP_SIZE_EXCEEDED"
    WITHIN_CONFIGURED_POLICY = "WITHIN_CONFIGURED_POLICY"


@dataclass(frozen=True, slots=True)
class RatioEvaluation:
    """The answer, with every number it was derived from.

    ``ratio_state`` is the headline; ``conditions`` lists *every* configured limit that is
    exceeded, so a room over both its ratio and its group size reports both rather than one
    hiding the other. Counts are ``None`` whenever they were not usable.
    """

    ratio_state: RatioState
    conditions: tuple[RatioState, ...]
    explanations: tuple[Explanation, ...]
    child_count: int | None
    staff_count: int | None
    max_children_per_staff: int | None
    minimum_staff: int | None
    required_staff: int | None
    staff_deficit: int | None
    group_size: int | None
    maximum_group_size: int | None
    child_freshness: Freshness
    staff_freshness: Freshness
    policy_id: UUID | None
    policy_revision: int | None
    evaluated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "ratio_state": str(self.ratio_state),
            "conditions": [str(value) for value in self.conditions],
            "explanations": [str(value) for value in self.explanations],
            "child_count": self.child_count,
            "staff_count": self.staff_count,
            "max_children_per_staff": self.max_children_per_staff,
            "minimum_staff": self.minimum_staff,
            "required_staff": self.required_staff,
            "staff_deficit": self.staff_deficit,
            "group_size": self.group_size,
            "maximum_group_size": self.maximum_group_size,
            "child_freshness": str(self.child_freshness),
            "staff_freshness": str(self.staff_freshness),
            "policy_id": None if self.policy_id is None else str(self.policy_id),
            "policy_revision": self.policy_revision,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def required_staff(child_count: int, max_children_per_staff: int, minimum_staff: int) -> int:
    """``max(minimum_staff, ceil(children / max_children_per_staff))`` when children are
    present; 0 when none are, because an empty room needs no staff for ratio purposes."""
    if child_count <= 0:
        return 0
    return max(minimum_staff, math.ceil(child_count / max_children_per_staff))


def evaluate_ratio(
    policy: RatioPolicyTerms | None,
    presence: PresenceSnapshot | None,
    now: datetime,
    *,
    classroom_active: bool = True,
    policy_ambiguous: bool = False,
) -> RatioEvaluation:
    """Evaluate one classroom at ``now``. Pure and total: every input yields a state."""
    _require_aware(now, "evaluation_time_must_be_utc_aware")
    if presence is not None and not isinstance(presence, PresenceSnapshot):
        # A VisionObservation (or anything else) is refused outright, not coerced.
        raise RatioPolicyError("presence_must_come_from_an_approved_source")
    children = None if presence is None else presence.children
    staff = None if presence is None else presence.qualified_staff
    child_fresh = freshness(children, now)
    staff_fresh = freshness(staff, now)

    def result(
        state: RatioState,
        explanations: Sequence[Explanation],
        *,
        conditions: Sequence[RatioState] = (),
        child_count: int | None = None,
        staff_count: int | None = None,
        needed: int | None = None,
        deficit: int | None = None,
    ) -> RatioEvaluation:
        return RatioEvaluation(
            ratio_state=state,
            conditions=tuple(conditions),
            explanations=tuple(explanations),
            child_count=child_count,
            staff_count=staff_count,
            max_children_per_staff=None if policy is None else policy.max_children_per_staff,
            minimum_staff=None if policy is None else policy.minimum_staff,
            required_staff=needed,
            staff_deficit=deficit,
            group_size=child_count,
            maximum_group_size=None if policy is None else policy.maximum_group_size,
            child_freshness=child_fresh,
            staff_freshness=staff_fresh,
            policy_id=None if policy is None else policy.policy_id,
            policy_revision=None if policy is None else policy.revision,
            evaluated_at=now,
        )

    if not classroom_active:
        return result(RatioState.NOT_CONFIGURED, [Explanation.CLASSROOM_INACTIVE])
    if policy is None or not policy.applies_at(now):
        return result(RatioState.NOT_CONFIGURED, [Explanation.POLICY_NOT_CONFIGURED])

    prefix = [Explanation.POLICY_SELECTION_AMBIGUOUS] if policy_ambiguous else []
    missing: list[Explanation] = []
    if child_fresh is Freshness.MISSING:
        missing.append(Explanation.CHILD_COUNT_MISSING)
    elif child_fresh is Freshness.STALE:
        missing.append(Explanation.CHILD_COUNT_STALE)
    if staff_fresh is Freshness.MISSING:
        missing.append(Explanation.STAFF_COUNT_MISSING)
    elif staff_fresh is Freshness.STALE:
        missing.append(Explanation.STAFF_COUNT_STALE)
    if missing:
        # Nothing is carried forward: not the last safe state, not the last violation.
        return result(RatioState.INSUFFICIENT_DATA, [*prefix, *missing])

    assert children is not None and staff is not None  # both FRESH
    child_count, staff_count = children.count, staff.count
    if child_count == 0:
        return result(
            RatioState.NO_CHILDREN_PRESENT,
            [*prefix, Explanation.NO_CHILDREN_PRESENT],
            child_count=0,
            staff_count=staff_count,
            needed=0,
            deficit=0,
        )

    needed = required_staff(child_count, policy.max_children_per_staff, policy.minimum_staff)
    deficit = max(0, needed - staff_count)
    conditions: list[RatioState] = []
    explanations: list[Explanation] = list(prefix)
    if deficit > 0:
        conditions.append(RatioState.OVER_CONFIGURED_RATIO)
        explanations.append(
            Explanation.NO_QUALIFIED_STAFF_PRESENT
            if staff_count == 0
            else Explanation.STAFF_BELOW_REQUIRED
        )
    if policy.maximum_group_size is not None and child_count > policy.maximum_group_size:
        conditions.append(RatioState.OVER_CONFIGURED_GROUP_SIZE)
        explanations.append(Explanation.GROUP_SIZE_EXCEEDED)
    if not conditions:
        explanations.append(Explanation.WITHIN_CONFIGURED_POLICY)
    return result(
        conditions[0] if conditions else RatioState.WITHIN_CONFIGURED_POLICY,
        explanations,
        conditions=conditions,
        child_count=child_count,
        staff_count=staff_count,
        needed=needed,
        deficit=deficit,
    )


# -------------------------------------------------------------------------- reconciliation
class ReconciliationState(StrEnum):
    NOT_AVAILABLE = "NOT_AVAILABLE"
    AGREES = "AGREES"
    VISION_LOWER_THAN_ROSTER = "VISION_LOWER_THAN_ROSTER"
    VISION_HIGHER_THAN_ROSTER = "VISION_HIGHER_THAN_ROSTER"


@dataclass(frozen=True, slots=True)
class VisionReconciliation:
    """How the camera's head count compares with the authoritative total. Diagnostic only.

    ``unexplained_observed_people`` is people the camera saw that no approved source accounts
    for. They are *unexplained*, nothing more: not children, not staff, not visitors.
    """

    state: ReconciliationState
    authoritative_expected_people: int | None
    vision_observed_people: int | None
    difference: int | None
    unexplained_observed_people: int | None
    unseen_expected_people: int | None
    visitors_included: bool
    reasons: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "authoritative_expected_people": self.authoritative_expected_people,
            "vision_observed_people": self.vision_observed_people,
            "difference": self.difference,
            "unexplained_observed_people": self.unexplained_observed_people,
            "unseen_expected_people": self.unseen_expected_people,
            "visitors_included": self.visitors_included,
            "reasons": list(self.reasons),
        }


def reconcile_vision(
    presence: PresenceSnapshot | None, vision: VisionObservation | None, now: datetime
) -> VisionReconciliation:
    """Compare ``children + qualified staff (+ visitors when supplied)`` with the camera.

    Returns a new value and mutates nothing; there is no code path from here back into a
    presence count or a ratio evaluation.
    """
    _require_aware(now, "evaluation_time_must_be_utc_aware")

    def unavailable(*reasons: str) -> VisionReconciliation:
        return VisionReconciliation(
            ReconciliationState.NOT_AVAILABLE, None, None, None, None, None, False, reasons
        )

    if vision is None:
        return unavailable("VISION_NOT_CONNECTED")
    if vision.observed_at - now > MAX_FUTURE_SKEW or now >= vision.observed_at + timedelta(
        seconds=vision.valid_for_seconds
    ):
        return unavailable("VISION_STALE")
    if presence is None:
        return unavailable("PRESENCE_NOT_CONNECTED")
    if presence.classroom_id != vision.classroom_id:
        return unavailable("CLASSROOM_MISMATCH")
    children, staff = presence.children, presence.qualified_staff
    reasons: list[str] = []
    if freshness(children, now) is not Freshness.FRESH:
        reasons.append("CHILD_COUNT_NOT_FRESH")
    if freshness(staff, now) is not Freshness.FRESH:
        reasons.append("STAFF_COUNT_NOT_FRESH")
    if reasons:
        return unavailable(*reasons)
    assert children is not None and staff is not None
    visitors = presence.visitors
    visitors_included = freshness(visitors, now) is Freshness.FRESH
    expected = (
        children.count + staff.count + (visitors.count if visitors_included and visitors else 0)
    )
    difference = vision.observed_people - expected
    if difference == 0:
        state = ReconciliationState.AGREES
    elif difference > 0:
        state = ReconciliationState.VISION_HIGHER_THAN_ROSTER
    else:
        state = ReconciliationState.VISION_LOWER_THAN_ROSTER
    return VisionReconciliation(
        state=state,
        authoritative_expected_people=expected,
        vision_observed_people=vision.observed_people,
        difference=difference,
        unexplained_observed_people=max(0, difference),
        unseen_expected_people=max(0, -difference),
        visitors_included=visitors_included,
        reasons=() if visitors_included else ("VISITOR_COUNT_NOT_SUPPLIED",),
    )


# ================================================================ manual presence (V1-04B)
# The first authoritative presence source: an operator reports aggregate counts for a room.
# Counts only - no names, no identities, no images. Bounds are for one classroom and are meant
# to reject typos ("600" for "6"), not to encode any regulation: 150 children covers a combined
# multipurpose room, 50 staff and 50 visitors cover an open day. Validity is short on purpose,
# because a head count describes a moment: 30 s to 15 min, default 2 min.
MANUAL_MAX_CHILDREN = 150
MANUAL_MAX_QUALIFIED_STAFF = 50
MANUAL_MAX_VISITORS = 50
MANUAL_MIN_VALIDITY_SECONDS = 30
MANUAL_MAX_VALIDITY_SECONDS = 15 * 60
MANUAL_DEFAULT_VALIDITY_SECONDS = 120


class PresenceAvailability(StrEnum):
    """Why an authoritative presence snapshot is, or is not, available right now."""

    PRESENCE_FRESH = "PRESENCE_FRESH"
    PRESENCE_NOT_CONNECTED = "PRESENCE_NOT_CONNECTED"  # nothing has ever been reported
    PRESENCE_STALE = "PRESENCE_STALE"  # the latest report has expired
    PRESENCE_REVOKED = "PRESENCE_REVOKED"  # the latest report was withdrawn
    PRESENCE_NOT_YET_VALID = "PRESENCE_NOT_YET_VALID"  # timestamped beyond the permitted skew


def validate_manual_counts(children: int, qualified_staff: int | None, visitors: int) -> None:
    """``qualified_staff`` is None only for a roster-mode report (V1-04C): the staff count then
    comes from the check-in roster and the manual report carries none."""
    checks: list[tuple[int, int, str]] = [(children, MANUAL_MAX_CHILDREN, "invalid_child_count")]
    if qualified_staff is not None:
        checks.append(
            (qualified_staff, MANUAL_MAX_QUALIFIED_STAFF, "invalid_qualified_staff_count")
        )
    checks.append((visitors, MANUAL_MAX_VISITORS, "invalid_visitor_count"))
    for value, maximum, category in checks:
        if not _strict_int(value) or not 0 <= value <= maximum:
            raise RatioPolicyError(category)


def validate_manual_validity(seconds: int) -> None:
    if not _strict_int(seconds) or not (
        MANUAL_MIN_VALIDITY_SECONDS <= seconds <= MANUAL_MAX_VALIDITY_SECONDS
    ):
        raise RatioPolicyError("invalid_validity_seconds")


@dataclass(frozen=True, slots=True)
class ManualPresenceRecord:
    """One stored operator report, as the domain sees it. Aggregate counts and provenance only."""

    snapshot_id: UUID
    classroom_id: UUID
    child_count: int
    qualified_staff_count: int | None  # None: reported in roster mode (V1-04C)
    visitor_count: int
    observed_at: datetime
    valid_until: datetime
    created_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_manual_counts(self.child_count, self.qualified_staff_count, self.visitor_count)
        for value in (self.observed_at, self.valid_until, self.created_at):
            _require_aware(value, "presence_timestamp_must_be_utc_aware")
        seconds = (self.valid_until - self.observed_at).total_seconds()
        if seconds != int(seconds):
            raise RatioPolicyError("invalid_validity_seconds")
        validate_manual_validity(int(seconds))

    @property
    def valid_for_seconds(self) -> int:
        return int((self.valid_until - self.observed_at).total_seconds())

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def ordering_key(self) -> tuple[datetime, datetime, str]:
        """``observed_at DESC, created_at DESC, id DESC`` - the one authoritative ordering,
        shared with the SQL that fetches the latest row. Ties are broken by id, so the same
        rows always select the same snapshot."""
        return (self.observed_at, self.created_at, str(self.snapshot_id))

    def to_snapshot(self) -> PresenceSnapshot:
        """Explicit role slots from an explicit MANUAL source. Nothing else is filled: there is
        no UNKNOWN count here, and nothing from a camera. A roster-mode report has no staff
        count, so its staff slot stays empty rather than becoming zero."""

        def count(role: PresenceRole, value: int) -> PresenceCount:
            return PresenceCount(
                self.classroom_id,
                role,
                value,
                PresenceSource.MANUAL,
                self.observed_at,
                self.valid_for_seconds,
            )

        return PresenceSnapshot(
            self.classroom_id,
            children=count(PresenceRole.CHILD, self.child_count),
            qualified_staff=None
            if self.qualified_staff_count is None
            else count(PresenceRole.QUALIFIED_STAFF, self.qualified_staff_count),
            visitors=count(PresenceRole.VISITOR, self.visitor_count),
        )


@dataclass(frozen=True, slots=True)
class PresenceResolution:
    """Which stored report, if any, is authoritative now, and why."""

    availability: PresenceAvailability
    record: ManualPresenceRecord | None
    snapshot: PresenceSnapshot | None

    @property
    def connected(self) -> bool:
        return self.availability is not PresenceAvailability.PRESENCE_NOT_CONNECTED


def resolve_manual_presence(
    records: Iterable[ManualPresenceRecord], classroom_id: UUID, now: datetime
) -> PresenceResolution:
    """The latest report for this classroom decides; nothing older is ever reconsidered.

    Supersession is permanent. If the latest report is revoked or stale, the answer is that -
    an earlier report is never resurrected, because an operator who withdraws or lets lapse
    the current count must not find an older, silently re-authoritative one in its place.
    Only a FRESH latest report yields a ``PresenceSnapshot`` for the ratio engine; a stale
    report's snapshot is passed through so the engine itself reports it as stale.
    """
    _require_aware(now, "evaluation_time_must_be_utc_aware")
    own = [record for record in records if record.classroom_id == classroom_id]
    if not own:
        return PresenceResolution(PresenceAvailability.PRESENCE_NOT_CONNECTED, None, None)
    latest = max(own, key=ManualPresenceRecord.ordering_key)
    if latest.revoked:
        return PresenceResolution(PresenceAvailability.PRESENCE_REVOKED, latest, None)
    snapshot = latest.to_snapshot()
    state = freshness(snapshot.children, now)
    if state is Freshness.FRESH:
        return PresenceResolution(PresenceAvailability.PRESENCE_FRESH, latest, snapshot)
    if latest.observed_at - now > MAX_FUTURE_SKEW:
        return PresenceResolution(PresenceAvailability.PRESENCE_NOT_YET_VALID, latest, snapshot)
    return PresenceResolution(PresenceAvailability.PRESENCE_STALE, latest, snapshot)
