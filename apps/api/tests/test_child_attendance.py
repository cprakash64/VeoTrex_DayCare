"""The pure child roster and attendance core (V1-04D): roster text, lifecycle, check-in/out
transitions, the ATTENDANCE child-count resolver, and attendance-mode source precedence.

No database and no clock. Every child here is a synthetic opaque id; no name reaches the
resolver, which is part of what these tests pin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature
from uuid import UUID, uuid4

import pytest

from veotrex_api.child_attendance import (
    ATTENDANCE_LEASE_DEFAULT_SECONDS,
    ATTENDANCE_LEASE_MAX_SECONDS,
    ATTENDANCE_LEASE_MIN_SECONDS,
    AttendanceEventRecord,
    AttendanceEventType,
    AttendanceState,
    AttendanceTransitionKind,
    ChildAttendanceError,
    ChildCountResolution,
    ChildRosterMember,
    ChildStatus,
    CurrentAttendance,
    clean_display_name,
    clean_external_reference,
    current_attendance,
    plan_check_in,
    plan_check_out,
    plan_refresh,
    resolve_child_count,
    status_transition,
    validate_attendance_lease,
)
from veotrex_api.classroom_ratio import (
    MAX_VALIDITY_SECONDS,
    Freshness,
    ManualPresenceRecord,
    PresenceCount,
    PresenceResolution,
    PresenceRole,
    PresenceSnapshot,
    PresenceSource,
    RatioEvaluation,
    RatioPolicyError,
    RatioPolicyTerms,
    RatioState,
    ReconciliationState,
    VisionObservation,
    evaluate_ratio,
    reconcile_vision,
    required_staff,
    resolve_manual_presence,
)
from veotrex_api.staff_presence import (
    CompositePresence,
    EligibilityTerms,
    PresenceSourceMode,
    RosterMember,
    StaffCountResolution,
    StaffPresenceEventRecord,
    StaffPresenceEventType,
    compose_presence,
    resolve_staff_count,
)

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
FACILITY = UUID("00000000-0000-4000-8000-00000000f001")
OTHER_FACILITY = UUID("00000000-0000-4000-8000-00000000f002")
ROOM_X = UUID("00000000-0000-4000-8000-0000000000a1")
ROOM_Y = UUID("00000000-0000-4000-8000-0000000000a2")
ATTENDANCE = PresenceSourceMode.ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF


def event(
    child: UUID,
    sequence: int,
    kind: AttendanceEventType = AttendanceEventType.CHECKED_IN,
    *,
    room: UUID = ROOM_X,
    at: datetime = NOW - timedelta(hours=1),
    lease: int = ATTENDANCE_LEASE_DEFAULT_SECONDS,
    checked_in_at: datetime | None = None,
) -> AttendanceEventRecord:
    return AttendanceEventRecord(
        uuid4(),
        child,
        FACILITY,
        room,
        sequence,
        kind,
        at,
        None if kind is AttendanceEventType.CHECKED_OUT else at + timedelta(seconds=lease),
        checked_in_at or at,
    )


def member(child: UUID, status: ChildStatus = ChildStatus.ACTIVE) -> ChildRosterMember:
    return ChildRosterMember(child, FACILITY, status)


def current(child: UUID, stream: list[AttendanceEventRecord]) -> CurrentAttendance:
    return current_attendance(stream, child, NOW)


def count(
    stream: list[AttendanceEventRecord],
    members: list[ChildRosterMember],
    room: UUID = ROOM_X,
    now: datetime = NOW,
) -> ChildCountResolution:
    return resolve_child_count(
        classroom_id=room, facility_id=FACILITY, now=now, members=members, events=stream
    )


# ================================================================================ roster text
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Child A", "Child A"),
        ("  Child   A  ", "Child A"),
        ("Zo\u00eb", "Zo\u00eb"),
        ("Zoe\u0308", "Zo\u00eb"),  # decomposed input is NFC-normalised
        ("\u674e \u5c0f\u660e", "\u674e \u5c0f\u660e"),
        ("O'Brien-Smith", "O'Brien-Smith"),
    ],
)
def test_display_names_are_normalised(raw: str, expected: str) -> None:
    assert clean_display_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "A" * 121,
        "Child\u0000A",
        "Child\tA",
        "Child\nA",
        "Child\u200bA",  # zero-width space
        "Child\u202eA",  # right-to-left override
        "<script>alert(1)</script>",
        "Child <b>A</b>",
    ],
)
def test_bad_display_names_are_refused(raw: str) -> None:
    with pytest.raises(ChildAttendanceError, match="invalid_display_name"):
        clean_display_name(raw)


def test_the_longest_name_is_accepted() -> None:
    assert clean_display_name("A" * 120) == "A" * 120


@pytest.mark.parametrize("raw", ["SIS-1042", "roster:2026/a.b_c", "7"])
def test_external_references_are_identifiers(raw: str) -> None:
    assert clean_external_reference(raw) == raw
    assert clean_external_reference("  ") is None
    assert clean_external_reference(None) is None


@pytest.mark.parametrize("raw", ["Child A", "-leading", "a" * 65, "ref\u00e9", "ref;drop", "x<y"])
def test_external_references_cannot_hold_a_name_or_markup(raw: str) -> None:
    with pytest.raises(ChildAttendanceError, match="invalid_external_reference"):
        clean_external_reference(raw)


# ================================================================================ lifecycle
def test_lifecycle_active_inactive_archived() -> None:
    assert status_transition(ChildStatus.ACTIVE, ChildStatus.INACTIVE) is True
    assert status_transition(ChildStatus.INACTIVE, ChildStatus.ACTIVE) is True
    assert status_transition(ChildStatus.ACTIVE, ChildStatus.ARCHIVED) is True
    assert status_transition(ChildStatus.INACTIVE, ChildStatus.ARCHIVED) is True
    assert status_transition(ChildStatus.ACTIVE, ChildStatus.ACTIVE) is False
    assert status_transition(ChildStatus.ARCHIVED, ChildStatus.ARCHIVED) is False
    for target in (ChildStatus.ACTIVE, ChildStatus.INACTIVE):
        with pytest.raises(ChildAttendanceError, match="child_archived"):
            status_transition(ChildStatus.ARCHIVED, target)


# ==================================================================================== lease
def test_lease_bounds_follow_the_engines_twelve_hour_limit() -> None:
    assert ATTENDANCE_LEASE_MIN_SECONDS == 30 * 60
    assert ATTENDANCE_LEASE_MAX_SECONDS == 12 * 60 * 60 == MAX_VALIDITY_SECONDS
    assert ATTENDANCE_LEASE_DEFAULT_SECONDS <= ATTENDANCE_LEASE_MAX_SECONDS
    for seconds in (ATTENDANCE_LEASE_MIN_SECONDS, ATTENDANCE_LEASE_MAX_SECONDS):
        validate_attendance_lease(seconds)


@pytest.mark.parametrize("seconds", [0, 60, 29 * 60, 12 * 3600 + 1, 18 * 3600, 86400, True, "3600"])
def test_excessive_or_short_leases_are_refused(seconds: object) -> None:
    with pytest.raises(ChildAttendanceError, match="invalid_lease_seconds"):
        validate_attendance_lease(seconds)  # type: ignore[arg-type]


def test_an_open_event_without_a_bounded_lease_cannot_exist() -> None:
    with pytest.raises(ChildAttendanceError):
        AttendanceEventRecord(
            uuid4(), uuid4(), FACILITY, ROOM_X, 1, AttendanceEventType.CHECKED_IN, NOW, None, NOW
        )
    with pytest.raises(ChildAttendanceError):
        event(uuid4(), 1, lease=18 * 3600)


# =========================================================================== transitions
def plan_in(child: UUID, stream: list[AttendanceEventRecord], **overrides: object) -> object:
    arguments: dict[str, object] = {
        "classroom_id": ROOM_X,
        "facility_id": FACILITY,
        "child_facility_id": FACILITY,
        "now": NOW,
        "lease_seconds": ATTENDANCE_LEASE_DEFAULT_SECONDS,
        "child_status": ChildStatus.ACTIVE,
        "classroom_active": True,
    }
    arguments.update(overrides)
    return plan_check_in(current(child, stream), **arguments)  # type: ignore[arg-type]


def test_valid_check_in_appends_one_leased_event() -> None:
    child = uuid4()
    transition = plan_check_in(
        current(child, []),
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        child_facility_id=FACILITY,
        now=NOW,
        lease_seconds=8 * 3600,
        child_status=ChildStatus.ACTIVE,
        classroom_active=True,
    )
    assert transition.kind is AttendanceTransitionKind.CHECKED_IN
    (only,) = transition.events
    assert (only.event_type, only.sequence, only.checked_in_at) == (
        AttendanceEventType.CHECKED_IN,
        1,
        NOW,
    )
    assert only.valid_until == NOW + timedelta(hours=8)


def test_duplicate_same_room_check_in_is_a_deterministic_conflict() -> None:
    child = uuid4()
    with pytest.raises(ChildAttendanceError, match="child_already_checked_in"):
        plan_in(child, [event(child, 1)])
    lapsed = [event(child, 1, at=NOW - timedelta(hours=13))]
    assert plan_in(child, lapsed).kind is AttendanceTransitionKind.CHECKED_IN  # type: ignore[attr-defined]


def test_a_move_is_one_atomic_check_out_then_check_in() -> None:
    child = uuid4()
    started = NOW - timedelta(hours=2)
    transition = plan_in(child, [event(child, 1, at=started)], classroom_id=ROOM_Y)
    assert transition.kind is AttendanceTransitionKind.MOVED  # type: ignore[attr-defined]
    leave, arrive = transition.events  # type: ignore[attr-defined]
    assert (leave.event_type, leave.classroom_id, leave.sequence, leave.checked_in_at) == (
        AttendanceEventType.CHECKED_OUT,
        ROOM_X,
        2,
        started,
    )
    assert (arrive.event_type, arrive.classroom_id, arrive.sequence) == (
        AttendanceEventType.CHECKED_IN,
        ROOM_Y,
        3,
    )
    assert leave.occurred_at == arrive.occurred_at


@pytest.mark.parametrize(
    ("override", "category"),
    [
        ({"child_status": ChildStatus.INACTIVE}, "child_not_active"),
        ({"child_status": ChildStatus.ARCHIVED}, "child_archived"),
        ({"classroom_active": False}, "classroom_inactive"),
        ({"lease_seconds": 60}, "invalid_lease_seconds"),
        ({"lease_seconds": 18 * 3600}, "invalid_lease_seconds"),
        ({"child_facility_id": OTHER_FACILITY}, "child_facility_mismatch"),
    ],
)
def test_check_in_refusals(override: dict[str, object], category: str) -> None:
    with pytest.raises(ChildAttendanceError, match=category):
        plan_in(uuid4(), [], **override)


def test_check_out_and_its_idempotent_repeat() -> None:
    child = uuid4()
    first = plan_check_out(current(child, [event(child, 1)]), classroom_id=ROOM_X, now=NOW)
    assert first.kind is AttendanceTransitionKind.CHECKED_OUT
    closed = [event(child, 1), event(child, 2, AttendanceEventType.CHECKED_OUT, at=NOW)]
    again = plan_check_out(current(child, closed), classroom_id=ROOM_X, now=NOW)
    assert again.kind is AttendanceTransitionKind.UNCHANGED and again.events == ()


def test_wrong_room_check_out_is_a_conflict() -> None:
    child = uuid4()
    with pytest.raises(ChildAttendanceError, match="child_in_another_classroom"):
        plan_check_out(current(child, [event(child, 1, room=ROOM_Y)]), classroom_id=ROOM_X, now=NOW)


def test_refresh_before_expiry_extends_and_after_expiry_is_refused() -> None:
    child = uuid4()
    started = NOW - timedelta(hours=11)
    transition = plan_refresh(
        current(child, [event(child, 1, at=started)]),
        classroom_id=ROOM_X,
        now=NOW,
        lease_seconds=4 * 3600,
        child_status=ChildStatus.ACTIVE,
        classroom_active=True,
    )
    (refreshed,) = transition.events
    assert refreshed.event_type is AttendanceEventType.REFRESHED
    assert refreshed.valid_until == NOW + timedelta(hours=4)
    assert refreshed.checked_in_at == started
    expired = [event(child, 1, at=NOW - timedelta(hours=13))]
    with pytest.raises(ChildAttendanceError, match="attendance_expired"):
        plan_refresh(
            current(child, expired),
            classroom_id=ROOM_X,
            now=NOW,
            lease_seconds=3600,
            child_status=ChildStatus.ACTIVE,
            classroom_active=True,
        )
    with pytest.raises(ChildAttendanceError, match="child_not_checked_in"):
        plan_refresh(
            current(child, []),
            classroom_id=ROOM_X,
            now=NOW,
            lease_seconds=3600,
            child_status=ChildStatus.ACTIVE,
            classroom_active=True,
        )


def test_another_childs_events_never_affect_this_child() -> None:
    child, other = uuid4(), uuid4()
    stream = [event(other, 1), event(other, 2, AttendanceEventType.CHECKED_OUT, at=NOW)]
    assert current(child, stream).state is AttendanceState.NOT_CHECKED_IN
    assert current(child, stream).sequence == 0


def test_yesterdays_attendance_never_counts_today() -> None:
    child = uuid4()
    yesterday = [event(child, 1, at=NOW - timedelta(days=1))]
    assert current(child, yesterday).state is AttendanceState.STALE
    assert count(yesterday, [member(child)]).count == 0


# ================================================================================ resolver
def test_zero_one_and_six_children() -> None:
    assert count([], []).count == 0
    one = uuid4()
    assert count([event(one, 1)], [member(one)]).count == 1
    six = [uuid4() for _ in range(6)]
    result = count([event(child, 1) for child in six], [member(child) for child in six])
    assert (result.count, result.present, result.source) == (6, 6, PresenceSource.ATTENDANCE)
    assert result.freshness is Freshness.FRESH


@pytest.mark.parametrize(
    "case", ["stale", "inactive", "archived", "checked_out", "other_room", "other_facility"]
)
def test_a_child_counts_only_when_every_condition_holds(case: str) -> None:
    child = uuid4()
    stream = [event(child, 1)]
    members = [member(child)]
    if case == "stale":
        stream = [event(child, 1, at=NOW - timedelta(hours=13))]
    elif case == "inactive":
        members = [member(child, ChildStatus.INACTIVE)]
    elif case == "archived":
        members = [member(child, ChildStatus.ARCHIVED)]
    elif case == "checked_out":
        stream = [event(child, 1), event(child, 2, AttendanceEventType.CHECKED_OUT, at=NOW)]
    elif case == "other_room":
        stream = [event(child, 1, room=ROOM_Y)]
    elif case == "other_facility":
        members = [ChildRosterMember(child, OTHER_FACILITY, ChildStatus.ACTIVE)]
    assert count(stream, members).count == 0


def test_a_moved_child_counts_in_the_new_room_only() -> None:
    child = uuid4()
    moved = [
        event(child, 1, at=NOW - timedelta(hours=2)),
        event(child, 2, AttendanceEventType.CHECKED_OUT, at=NOW - timedelta(hours=1)),
        event(child, 3, room=ROOM_Y, at=NOW - timedelta(hours=1)),
    ]
    assert count(moved, [member(child)], ROOM_X).count == 0
    assert count(moved, [member(child)], ROOM_Y).count == 1


def test_diagnostics_are_bounded_and_nameless() -> None:
    fresh, stale, inactive = uuid4(), uuid4(), uuid4()
    result = count(
        [event(fresh, 1), event(stale, 1, at=NOW - timedelta(hours=13)), event(inactive, 1)],
        [member(fresh), member(stale), member(inactive, ChildStatus.INACTIVE)],
    )
    assert (result.count, result.present, result.present_inactive, result.stale) == (1, 2, 1, 1)
    assert set(result.as_dict()) == {
        "source",
        "count",
        "present",
        "present_inactive",
        "stale",
        "freshness",
        "valid_until",
        "evaluated_at",
    }
    assert result.to_presence_count().source is PresenceSource.ATTENDANCE
    assert result.to_presence_count().role is PresenceRole.CHILD


def test_the_resolver_accepts_no_name_camera_track_or_recognition_input() -> None:
    assert set(signature(resolve_child_count).parameters) == {
        "classroom_id",
        "facility_id",
        "now",
        "members",
        "events",
    }
    assert set(ChildRosterMember.__dataclass_fields__) == {
        "child_profile_id",
        "facility_id",
        "status",
    }
    with pytest.raises(TypeError):
        resolve_child_count(  # type: ignore[call-arg]
            classroom_id=ROOM_X,
            facility_id=FACILITY,
            now=NOW,
            members=[],
            events=[],
            vision=VisionObservation(ROOM_X, 9, NOW),
        )


# ========================================================================= composition
POLICY = RatioPolicyTerms(
    policy_id=uuid4(),
    classroom_id=ROOM_X,
    label="Configured",
    max_children_per_staff=5,
    minimum_staff=0,
    maximum_group_size=None,
    effective_from=NOW - timedelta(days=1),
    effective_until=None,
    active=True,
    revision=1,
)


def roster(staff: int) -> StaffCountResolution:
    ids = [uuid4() for _ in range(staff)]
    return resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        members=[RosterMember(item, True) for item in ids],
        events=[
            StaffPresenceEventRecord(
                uuid4(),
                item,
                FACILITY,
                ROOM_X,
                1,
                StaffPresenceEventType.CHECKED_IN,
                NOW - timedelta(minutes=1),
                NOW + timedelta(minutes=14),
                NOW - timedelta(minutes=1),
            )
            for item in ids
        ],
        assignments=[
            EligibilityTerms(uuid4(), item, FACILITY, True, True, NOW - timedelta(days=1), None, 1)
            for item in ids
        ],
    )


def attendance(children: int) -> ChildCountResolution:
    ids = [uuid4() for _ in range(children)]
    return count([event(child, 1) for child in ids], [member(child) for child in ids])


def manual(
    children: int | None = None,
    staff: int | None = None,
    visitors: int = 0,
    *,
    observed: datetime = NOW - timedelta(seconds=10),
    validity: int = 120,
) -> PresenceResolution:
    record = ManualPresenceRecord(
        snapshot_id=uuid4(),
        classroom_id=ROOM_X,
        child_count=children,
        qualified_staff_count=staff,
        visitor_count=visitors,
        observed_at=observed,
        valid_until=observed + timedelta(seconds=validity),
        created_at=observed,
    )
    return resolve_manual_presence([record], ROOM_X, NOW)


NO_MANUAL = resolve_manual_presence([], ROOM_X, NOW)


def evaluate(
    children: int,
    staff: int,
    manual_resolution: PresenceResolution = NO_MANUAL,
) -> tuple[CompositePresence, RatioEvaluation]:
    composite = compose_presence(
        ROOM_X, ATTENDANCE, manual_resolution, roster(staff), NOW, attendance=attendance(children)
    )
    return composite, evaluate_ratio(POLICY, composite.snapshot, NOW)


@pytest.mark.parametrize(
    ("children", "staff", "state", "needed", "deficit"),
    [
        (5, 1, RatioState.WITHIN_CONFIGURED_POLICY, 1, 0),
        (6, 1, RatioState.OVER_CONFIGURED_RATIO, 2, 1),
        (6, 2, RatioState.WITHIN_CONFIGURED_POLICY, 2, 0),
        (1, 0, RatioState.OVER_CONFIGURED_RATIO, 1, 1),
        (0, 0, RatioState.NO_CHILDREN_PRESENT, 0, 0),
    ],
)
def test_attendance_children_and_roster_staff_feed_the_unchanged_engine(
    children: int, staff: int, state: RatioState, needed: int, deficit: int
) -> None:
    composite, result = evaluate(children, staff)
    assert result.ratio_state is state
    assert (result.child_count, result.staff_count) == (children, staff)
    assert (result.required_staff, result.staff_deficit) == (needed, deficit)
    assert composite.children.source is PresenceSource.ATTENDANCE
    assert composite.qualified_staff.source is PresenceSource.STAFF_ROSTER
    # The formula itself is untouched: max(minimum, ceil(children / ratio)).
    assert required_staff(children, 5, 0) == needed


def test_attendance_mode_needs_both_derived_sources() -> None:
    with pytest.raises(RatioPolicyError, match="child_attendance_required"):
        compose_presence(ROOM_X, ATTENDANCE, NO_MANUAL, roster(1), NOW)
    with pytest.raises(RatioPolicyError, match="staff_roster_required"):
        compose_presence(ROOM_X, ATTENDANCE, NO_MANUAL, None, NOW, attendance=attendance(1))


def test_manual_children_and_staff_are_never_added_to_attendance_or_roster() -> None:
    legacy = manual(children=9, staff=4, visitors=1)
    composite, result = evaluate(6, 1, legacy)
    assert (result.child_count, result.staff_count) == (6, 1), "not 15 children, not 5 staff"
    assert composite.visitors.count == 1 and composite.visitors.source is PresenceSource.MANUAL


def test_manual_mode_does_not_borrow_attendance() -> None:
    visitor_only = manual(children=None, staff=None, visitors=2)
    composite = compose_presence(
        ROOM_X,
        PresenceSourceMode.MANUAL_AGGREGATE,
        visitor_only,
        roster(2),
        NOW,
        attendance=attendance(6),
    )
    result = evaluate_ratio(POLICY, composite.snapshot, NOW)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert {"CHILD_COUNT_MISSING", "STAFF_COUNT_MISSING"} <= {str(x) for x in result.explanations}
    assert composite.attendance is None and composite.roster is None


def test_stale_attendance_is_excluded_by_event_freshness_not_carried() -> None:
    kids = [uuid4() for _ in range(6)]
    stream = [event(child, 1, lease=3600, at=NOW - timedelta(minutes=30)) for child in kids[:4]]
    stream += [event(child, 1, at=NOW - timedelta(hours=13)) for child in kids[4:]]
    resolution = count(stream, [member(child) for child in kids])
    composite = compose_presence(
        ROOM_X, ATTENDANCE, NO_MANUAL, roster(1), NOW, attendance=resolution
    )
    result = evaluate_ratio(POLICY, composite.snapshot, NOW)
    assert result.child_count == 4 and resolution.stale == 2
    assert result.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY


def test_missing_staff_in_manual_mode_still_reads_missing() -> None:
    children_only = manual(children=6, staff=None)
    composite = compose_presence(
        ROOM_X, PresenceSourceMode.MANUAL_AGGREGATE, children_only, None, NOW
    )
    result = evaluate_ratio(POLICY, composite.snapshot, NOW)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert "STAFF_COUNT_MISSING" in {str(x) for x in result.explanations}


# ================================================================================ visitors
def test_missing_visitors_never_block_the_ratio_and_are_never_zero() -> None:
    composite, result = evaluate(6, 2)
    assert result.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY
    assert composite.visitors.count is None
    assert composite.visitors.freshness is Freshness.MISSING
    reconciliation = reconcile_vision(composite.snapshot, VisionObservation(ROOM_X, 8, NOW), NOW)
    assert reconciliation.visitors_included is False
    assert "VISITOR_COUNT_NOT_SUPPLIED" in reconciliation.reasons


def test_stale_visitors_never_block_and_are_not_zero() -> None:
    stale = manual(visitors=3, observed=NOW - timedelta(minutes=20))
    composite, result = evaluate(6, 2, stale)
    assert result.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY
    assert composite.visitors.count is None and composite.visitors.freshness is Freshness.STALE


def test_fresh_manual_visitors_join_reconciliation() -> None:
    composite, _ = evaluate(6, 2, manual(visitors=1))
    assert composite.visitors.count == 1
    reconciliation = reconcile_vision(composite.snapshot, VisionObservation(ROOM_X, 9, NOW), NOW)
    assert reconciliation.visitors_included is True
    assert reconciliation.authoritative_expected_people == 9
    assert reconciliation.state is ReconciliationState.AGREES


def test_a_visitor_only_manual_report_is_fresh_on_its_own_window() -> None:
    assert manual(visitors=0).availability.value == "PRESENCE_FRESH"
    assert manual(visitors=0, observed=NOW - timedelta(minutes=5)).availability.value == (
        "PRESENCE_STALE"
    )


# ============================================================================ role safety
def test_unknown_and_vision_can_never_become_a_child() -> None:
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(
            ROOM_X,
            children=PresenceCount(ROOM_X, PresenceRole.UNKNOWN, 3, PresenceSource.MANUAL, NOW, 60),
        )
    with pytest.raises(RatioPolicyError):
        evaluate_ratio(POLICY, VisionObservation(ROOM_X, 9, NOW), NOW)  # type: ignore[arg-type]
    # ATTENDANCE may assert a child but never qualified staff.
    with pytest.raises(RatioPolicyError, match="source_cannot_assert_role"):
        PresenceCount(ROOM_X, PresenceRole.QUALIFIED_STAFF, 1, PresenceSource.ATTENDANCE, NOW, 60)
    # A recognition count is refused in the child slot of any composition.
    with pytest.raises(RatioPolicyError):
        PresenceCount(ROOM_X, PresenceRole.CHILD, 1, PresenceSource.STAFF_RECOGNITION, NOW, 60)
