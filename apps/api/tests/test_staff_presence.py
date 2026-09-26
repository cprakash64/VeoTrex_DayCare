"""The pure staff roster core (V1-04C): eligibility, check-in/out transitions, the qualified-staff
resolver and the composite source precedence.

No database and no clock: every test passes ``now``. Every person here is a synthetic adult staff
identifier; there is no child identity anywhere in this module by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from veotrex_api.classroom_ratio import (
    Freshness,
    ManualPresenceRecord,
    PresenceAvailability,
    PresenceCount,
    PresenceResolution,
    PresenceRole,
    PresenceSnapshot,
    PresenceSource,
    RatioEvaluation,
    RatioPolicyError,
    RatioPolicyTerms,
    RatioState,
    VisionObservation,
    evaluate_ratio,
    resolve_manual_presence,
)
from veotrex_api.staff_presence import (
    STAFF_LEASE_DEFAULT_SECONDS,
    STAFF_LEASE_MAX_SECONDS,
    STAFF_LEASE_MIN_SECONDS,
    CompositePresence,
    CurrentStaffPresence,
    EligibilityTerms,
    PresenceSourceMode,
    RosterMember,
    StaffCountResolution,
    StaffPresenceError,
    StaffPresenceEventRecord,
    StaffPresenceEventType,
    StaffPresenceState,
    TransitionKind,
    compose_presence,
    current_presence,
    plan_check_in,
    plan_check_out,
    plan_refresh,
    require_authoritative,
    resolve_staff_count,
    select_eligibility,
    validate_lease,
)

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
FACILITY = UUID("00000000-0000-4000-8000-00000000f001")
OTHER_FACILITY = UUID("00000000-0000-4000-8000-00000000f002")
ROOM_X = UUID("00000000-0000-4000-8000-0000000000a1")
ROOM_Y = UUID("00000000-0000-4000-8000-0000000000a2")
ROOM_Z = UUID("00000000-0000-4000-8000-0000000000a3")  # in OTHER_FACILITY


def terms(
    staff: UUID,
    *,
    counts: bool = True,
    active: bool = True,
    facility: UUID = FACILITY,
    start: datetime = NOW - timedelta(days=1),
    until: datetime | None = None,
    revision: int = 1,
) -> EligibilityTerms:
    return EligibilityTerms(uuid4(), staff, facility, counts, active, start, until, revision)


def event(
    staff: UUID,
    sequence: int,
    kind: StaffPresenceEventType,
    *,
    room: UUID = ROOM_X,
    facility: UUID = FACILITY,
    at: datetime = NOW - timedelta(minutes=5),
    lease: int = STAFF_LEASE_DEFAULT_SECONDS,
    checked_in_at: datetime | None = None,
) -> StaffPresenceEventRecord:
    return StaffPresenceEventRecord(
        uuid4(),
        staff,
        facility,
        room,
        sequence,
        kind,
        at,
        None if kind is StaffPresenceEventType.CHECKED_OUT else at + timedelta(seconds=lease),
        checked_in_at or at,
    )


def checked_in(staff: UUID, **kwargs: object) -> StaffPresenceEventRecord:
    return event(staff, 1, StaffPresenceEventType.CHECKED_IN, **kwargs)  # type: ignore[arg-type]


def count(
    staff_events: list[StaffPresenceEventRecord],
    assignments: list[EligibilityTerms],
    members: list[RosterMember],
    *,
    room: UUID = ROOM_X,
    now: datetime = NOW,
) -> int:
    return resolve_staff_count(
        classroom_id=room,
        facility_id=FACILITY,
        now=now,
        members=members,
        events=staff_events,
        assignments=assignments,
    ).count


def member(staff: UUID, active: bool = True) -> RosterMember:
    return RosterMember(staff, active)


def current(
    staff: UUID, staff_events: list[StaffPresenceEventRecord], now: datetime = NOW
) -> CurrentStaffPresence:
    return current_presence(staff_events, staff, now)


# ============================================================================ eligibility
def test_active_eligibility_applies_within_its_period_only() -> None:
    staff = uuid4()
    open_ended = terms(staff)
    assert open_ended.applies_at(NOW)
    future = terms(staff, start=NOW + timedelta(hours=1))
    assert not future.applies_at(NOW)
    expired = terms(staff, start=NOW - timedelta(days=2), until=NOW - timedelta(days=1))
    assert not expired.applies_at(NOW)
    inactive = terms(staff, active=False)
    assert not inactive.applies_at(NOW)


def test_select_eligibility_ignores_other_people_facilities_and_periods() -> None:
    staff, other = uuid4(), uuid4()
    candidates = [
        terms(other),
        terms(staff, facility=OTHER_FACILITY),
        terms(staff, start=NOW + timedelta(days=1)),
        terms(staff, active=False),
    ]
    assert select_eligibility(candidates, staff, FACILITY, NOW) is None
    wanted = terms(staff, counts=False)
    assert select_eligibility([*candidates, wanted], staff, FACILITY, NOW) == wanted


def test_overlapping_designations_are_resolved_deterministically_and_not_counted() -> None:
    staff = uuid4()
    older = terms(staff, counts=True, start=NOW - timedelta(days=3))
    newer = terms(staff, counts=False, start=NOW - timedelta(days=1))
    for order in ([older, newer], [newer, older]):
        assert select_eligibility(order, staff, FACILITY, NOW) == newer
    # Conflicting designations are ambiguous: the resolver does not count the person at all.
    resolution = resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        members=[member(staff)],
        events=[checked_in(staff)],
        assignments=[older, newer],
    )
    assert (resolution.count, resolution.present_ambiguous) == (0, 1)


@pytest.mark.parametrize("bad", ["inverted", "naive"])
def test_eligibility_periods_are_validated(bad: str) -> None:
    with pytest.raises(StaffPresenceError):
        if bad == "inverted":
            terms(uuid4(), until=NOW - timedelta(days=2))
        else:
            EligibilityTerms(uuid4(), uuid4(), FACILITY, True, True, datetime(2026, 1, 1), None, 1)


# =================================================================================== lease
@pytest.mark.parametrize(
    "seconds", [STAFF_LEASE_MIN_SECONDS, STAFF_LEASE_DEFAULT_SECONDS, STAFF_LEASE_MAX_SECONDS]
)
def test_leases_within_bounds_are_accepted(seconds: int) -> None:
    validate_lease(seconds)


@pytest.mark.parametrize(
    "seconds", [0, 59, STAFF_LEASE_MAX_SECONDS + 1, 24 * 3600, -1, True, 900.0, "900"]
)
def test_excessive_or_malformed_leases_are_refused(seconds: object) -> None:
    with pytest.raises(StaffPresenceError, match="invalid_lease_seconds"):
        validate_lease(seconds)  # type: ignore[arg-type]


def test_the_default_lease_is_fifteen_minutes_and_never_unbounded() -> None:
    assert STAFF_LEASE_DEFAULT_SECONDS == 15 * 60
    assert STAFF_LEASE_MAX_SECONDS == 4 * 60 * 60


def test_an_open_event_without_a_lease_cannot_exist() -> None:
    with pytest.raises(StaffPresenceError):
        StaffPresenceEventRecord(
            uuid4(),
            uuid4(),
            FACILITY,
            ROOM_X,
            1,
            StaffPresenceEventType.CHECKED_IN,
            NOW,
            None,
            NOW,
        )
    with pytest.raises(StaffPresenceError):
        event(uuid4(), 1, StaffPresenceEventType.CHECKED_IN, lease=STAFF_LEASE_MAX_SECONDS + 60)


# ========================================================================= current presence
def test_presence_is_derived_from_the_latest_event_only() -> None:
    staff = uuid4()
    assert current(staff, []).state is StaffPresenceState.NOT_CHECKED_IN
    stream = [
        event(staff, 1, StaffPresenceEventType.CHECKED_IN, at=NOW - timedelta(minutes=10)),
        event(staff, 2, StaffPresenceEventType.CHECKED_OUT, at=NOW - timedelta(minutes=5)),
    ]
    assert current(staff, stream).state is StaffPresenceState.NOT_CHECKED_IN
    # Order of input is irrelevant: the sequence decides.
    assert current(staff, list(reversed(stream))).state is StaffPresenceState.NOT_CHECKED_IN
    here = current(staff, [event(staff, 3, StaffPresenceEventType.CHECKED_IN), *stream])
    assert here.state is StaffPresenceState.PRESENT and here.classroom_id == ROOM_X
    assert here.sequence == 3


def test_a_lease_that_ran_out_is_stale_not_present() -> None:
    staff = uuid4()
    yesterday = [checked_in(staff, at=NOW - timedelta(days=1))]
    assert current(staff, yesterday).state is StaffPresenceState.STALE
    at_expiry = [checked_in(staff, at=NOW - timedelta(seconds=STAFF_LEASE_DEFAULT_SECONDS))]
    assert current(staff, at_expiry).state is StaffPresenceState.STALE


def test_a_timestamp_beyond_the_permitted_skew_is_not_trusted() -> None:
    staff = uuid4()
    assert current(staff, [checked_in(staff, at=NOW + timedelta(minutes=10))]).state is (
        StaffPresenceState.STALE
    )


# ============================================================================ transitions
def check_in_plan(
    staff: UUID,
    stream: list[StaffPresenceEventRecord],
    room: UUID = ROOM_X,
    facility: UUID = FACILITY,
    **overrides: object,
) -> TransitionKind:
    arguments: dict[str, object] = {
        "classroom_id": room,
        "facility_id": facility,
        "now": NOW,
        "lease_seconds": STAFF_LEASE_DEFAULT_SECONDS,
        "staff_active": True,
        "classroom_active": True,
        "eligibility": terms(staff, facility=facility),
    }
    arguments.update(overrides)
    transition = plan_check_in(current(staff, stream), **arguments)  # type: ignore[arg-type]
    return transition.kind


def test_valid_check_in_appends_one_leased_event() -> None:
    staff = uuid4()
    transition = plan_check_in(
        current(staff, []),
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        lease_seconds=600,
        staff_active=True,
        classroom_active=True,
        eligibility=terms(staff),
    )
    assert transition.kind is TransitionKind.CHECKED_IN
    (only,) = transition.events
    assert only.event_type is StaffPresenceEventType.CHECKED_IN
    assert (only.sequence, only.occurred_at, only.checked_in_at) == (1, NOW, NOW)
    assert only.valid_until == NOW + timedelta(seconds=600)


def test_a_ratio_ineligible_roster_member_may_check_in() -> None:
    staff = uuid4()
    assert check_in_plan(staff, [], eligibility=terms(staff, counts=False)) is (
        TransitionKind.CHECKED_IN
    )


def test_duplicate_check_in_to_the_same_room_is_a_deterministic_conflict() -> None:
    staff = uuid4()
    with pytest.raises(StaffPresenceError, match="staff_already_checked_in"):
        check_in_plan(staff, [checked_in(staff)])
    # A lapsed stay in the same room is simply checked in again.
    assert check_in_plan(staff, [checked_in(staff, at=NOW - timedelta(hours=1))]) is (
        TransitionKind.CHECKED_IN
    )


def test_room_transition_is_one_atomic_check_out_then_check_in() -> None:
    staff = uuid4()
    started = NOW - timedelta(minutes=5)
    transition = plan_check_in(
        current(staff, [checked_in(staff, at=started)]),
        classroom_id=ROOM_Y,
        facility_id=FACILITY,
        now=NOW,
        lease_seconds=900,
        staff_active=True,
        classroom_active=True,
        eligibility=terms(staff),
    )
    assert transition.kind is TransitionKind.MOVED
    leave, arrive = transition.events
    assert (leave.event_type, leave.classroom_id, leave.sequence) == (
        StaffPresenceEventType.CHECKED_OUT,
        ROOM_X,
        2,
    )
    assert leave.checked_in_at == started and leave.valid_until is None
    assert (arrive.event_type, arrive.classroom_id, arrive.sequence) == (
        StaffPresenceEventType.CHECKED_IN,
        ROOM_Y,
        3,
    )
    # Applying the plan leaves exactly one current room.
    stream = [checked_in(staff, at=started)] + [
        StaffPresenceEventRecord(
            uuid4(),
            staff,
            item.facility_id,
            item.classroom_id,
            item.sequence,
            item.event_type,
            item.occurred_at,
            item.valid_until,
            item.checked_in_at,
        )
        for item in transition.events
    ]
    after = current(staff, stream)
    assert after.classroom_id == ROOM_Y and after.state is StaffPresenceState.PRESENT


def test_a_present_stay_in_another_facility_blocks_check_in() -> None:
    staff = uuid4()
    elsewhere = [checked_in(staff, room=ROOM_Z, facility=OTHER_FACILITY)]
    with pytest.raises(StaffPresenceError, match="staff_checked_in_elsewhere"):
        check_in_plan(staff, elsewhere)
    lapsed = [checked_in(staff, room=ROOM_Z, facility=OTHER_FACILITY, at=NOW - timedelta(hours=2))]
    assert check_in_plan(staff, lapsed) is TransitionKind.CHECKED_IN


@pytest.mark.parametrize(
    ("override", "category"),
    [
        ({"staff_active": False}, "staff_not_active"),
        ({"classroom_active": False}, "classroom_inactive"),
        ({"eligibility": None}, "staff_not_assigned_to_facility"),
        ({"lease_seconds": 30}, "invalid_lease_seconds"),
        ({"lease_seconds": STAFF_LEASE_MAX_SECONDS + 1}, "invalid_lease_seconds"),
    ],
)
def test_check_in_refusals(override: dict[str, object], category: str) -> None:
    with pytest.raises(StaffPresenceError, match=category):
        check_in_plan(uuid4(), [], **override)  # type: ignore[arg-type]


def test_an_eligibility_at_another_facility_does_not_admit_a_check_in() -> None:
    staff = uuid4()
    with pytest.raises(StaffPresenceError, match="staff_not_assigned_to_facility"):
        check_in_plan(staff, [], eligibility=terms(staff, facility=OTHER_FACILITY))


def test_check_out_closes_the_stay_and_a_repeat_is_idempotent() -> None:
    staff = uuid4()
    first = plan_check_out(current(staff, [checked_in(staff)]), classroom_id=ROOM_X, now=NOW)
    assert first.kind is TransitionKind.CHECKED_OUT
    assert first.events[0].event_type is StaffPresenceEventType.CHECKED_OUT
    closed = [checked_in(staff), event(staff, 2, StaffPresenceEventType.CHECKED_OUT, at=NOW)]
    again = plan_check_out(current(staff, closed), classroom_id=ROOM_X, now=NOW)
    assert again.kind is TransitionKind.UNCHANGED and again.events == ()
    never = plan_check_out(current(staff, []), classroom_id=ROOM_X, now=NOW)
    assert never.kind is TransitionKind.UNCHANGED


def test_check_out_of_the_wrong_room_never_ends_a_real_stay() -> None:
    staff = uuid4()
    with pytest.raises(StaffPresenceError, match="staff_in_another_classroom"):
        plan_check_out(
            current(staff, [checked_in(staff, room=ROOM_Y)]), classroom_id=ROOM_X, now=NOW
        )


def test_refresh_extends_a_present_stay_by_a_bounded_lease() -> None:
    staff = uuid4()
    started = NOW - timedelta(minutes=10)
    transition = plan_refresh(
        current(staff, [checked_in(staff, at=started)]),
        classroom_id=ROOM_X,
        now=NOW,
        lease_seconds=1800,
        staff_active=True,
        classroom_active=True,
        eligibility=terms(staff),
    )
    (refreshed,) = transition.events
    assert refreshed.event_type is StaffPresenceEventType.REFRESHED
    assert refreshed.valid_until == NOW + timedelta(seconds=1800)
    assert refreshed.checked_in_at == started, "the stay keeps its original start"
    with pytest.raises(StaffPresenceError, match="invalid_lease_seconds"):
        plan_refresh(
            current(staff, [checked_in(staff, at=started)]),
            classroom_id=ROOM_X,
            now=NOW,
            lease_seconds=STAFF_LEASE_MAX_SECONDS + 1,
            staff_active=True,
            classroom_active=True,
            eligibility=terms(staff),
        )


@pytest.mark.parametrize(
    ("stream_kind", "category"),
    [
        ("none", "staff_not_checked_in"),
        ("other_room", "staff_not_checked_in"),
        ("stale", "staff_presence_expired"),
    ],
)
def test_refresh_refusals(stream_kind: str, category: str) -> None:
    staff = uuid4()
    stream = {
        "none": [],
        "other_room": [checked_in(staff, room=ROOM_Y)],
        "stale": [checked_in(staff, at=NOW - timedelta(hours=1))],
    }[stream_kind]
    with pytest.raises(StaffPresenceError, match=category):
        plan_refresh(
            current(staff, stream),
            classroom_id=ROOM_X,
            now=NOW,
            lease_seconds=900,
            staff_active=True,
            classroom_active=True,
            eligibility=terms(staff),
        )


# ================================================================================ resolver
def test_one_and_two_eligible_fresh_staff_count() -> None:
    alice, bob = uuid4(), uuid4()
    assignments = [terms(alice), terms(bob)]
    assert count([checked_in(alice)], assignments, [member(alice), member(bob)]) == 1
    both = [checked_in(alice), checked_in(bob)]
    assert count(both, assignments, [member(alice), member(bob)]) == 2


def test_the_toddler_room_example_counts_two_not_three() -> None:
    alice, bob, carol = uuid4(), uuid4(), uuid4()
    resolution = resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        members=[member(alice), member(bob), member(carol)],
        events=[checked_in(alice), checked_in(bob), checked_in(carol)],
        assignments=[terms(alice), terms(bob), terms(carol, counts=False)],
    )
    assert resolution.count == 2
    assert (resolution.present, resolution.present_ratio_ineligible) == (3, 1)
    assert resolution.source is PresenceSource.STAFF_ROSTER
    assert resolution.freshness is Freshness.FRESH
    assert set(resolution.counted_staff_ids) == {alice, bob}


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "checked_out",
        "other_classroom",
        "inactive_profile",
        "no_eligibility",
        "counts_false",
        "inactive_eligibility",
        "expired_eligibility",
        "future_eligibility",
        "other_facility_eligibility",
        "not_a_member",
    ],
)
def test_nobody_counts_unless_every_condition_holds(case: str) -> None:
    staff = uuid4()
    stream = [checked_in(staff)]
    assignments = [terms(staff)]
    members = [member(staff)]
    if case == "stale":
        stream = [checked_in(staff, at=NOW - timedelta(days=1))]
    elif case == "checked_out":
        stream = [checked_in(staff), event(staff, 2, StaffPresenceEventType.CHECKED_OUT, at=NOW)]
    elif case == "other_classroom":
        stream = [checked_in(staff, room=ROOM_Y)]
    elif case == "inactive_profile":
        members = [member(staff, active=False)]
    elif case == "no_eligibility":
        assignments = []
    elif case == "counts_false":
        assignments = [terms(staff, counts=False)]
    elif case == "inactive_eligibility":
        assignments = [terms(staff, active=False)]
    elif case == "expired_eligibility":
        assignments = [terms(staff, start=NOW - timedelta(days=5), until=NOW - timedelta(days=1))]
    elif case == "future_eligibility":
        assignments = [terms(staff, start=NOW + timedelta(hours=2))]
    elif case == "other_facility_eligibility":
        assignments = [terms(staff, facility=OTHER_FACILITY)]
    elif case == "not_a_member":
        members = []
    assert count(stream, assignments, members) == 0


def test_the_resolver_reports_why_people_did_not_count() -> None:
    fresh, stale, inactive = uuid4(), uuid4(), uuid4()
    resolution = resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        members=[member(fresh), member(stale), member(inactive, active=False)],
        events=[
            checked_in(fresh),
            checked_in(stale, at=NOW - timedelta(hours=2)),
            checked_in(inactive),
        ],
        assignments=[terms(fresh), terms(stale), terms(inactive)],
    )
    assert (resolution.count, resolution.stale, resolution.present_inactive) == (1, 1, 1)
    assert resolution.valid_until == NOW - timedelta(minutes=5) + timedelta(minutes=15)


def test_the_count_is_valid_until_the_earliest_counted_lease() -> None:
    early, late = uuid4(), uuid4()
    started = NOW - timedelta(seconds=30)
    stream = [checked_in(early, lease=120, at=started), checked_in(late, lease=3600, at=started)]
    resolution = resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=NOW,
        members=[member(early), member(late)],
        events=stream,
        assignments=[terms(early), terms(late)],
    )
    assert resolution.count == 2
    assert resolution.valid_until == started + timedelta(seconds=120)
    assert resolution.to_presence_count().valid_for_seconds == 90
    # Two minutes later that first lease has run out and the count drops by itself.
    later = count(
        stream,
        [terms(early), terms(late)],
        [member(early), member(late)],
        now=NOW + timedelta(minutes=2),
    )
    assert later == 1


# =============================================================================== composite
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


def manual(
    children: int = 6,
    staff: int | None = None,
    visitors: int = 0,
    *,
    observed: datetime = NOW - timedelta(seconds=10),
    validity: int = 120,
    revoked: bool = False,
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
        revoked_at=observed if revoked else None,
    )
    return resolve_manual_presence([record], ROOM_X, NOW)


def roster(*staff_ids: UUID, now: datetime = NOW) -> StaffCountResolution:
    return resolve_staff_count(
        classroom_id=ROOM_X,
        facility_id=FACILITY,
        now=now,
        members=[member(item) for item in staff_ids],
        events=[checked_in(item) for item in staff_ids],
        assignments=[terms(item) for item in staff_ids],
    )


def evaluate(
    mode: PresenceSourceMode, resolution: PresenceResolution, *staff: UUID
) -> tuple[CompositePresence, RatioEvaluation]:
    composite = compose_presence(
        ROOM_X,
        mode,
        resolution,
        roster(*staff) if mode is PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN else None,
        NOW,
    )
    return composite, evaluate_ratio(POLICY, composite.snapshot, NOW)


ROSTER = PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN


def test_manual_children_and_roster_staff_feed_the_unchanged_engine() -> None:
    composite, result = evaluate(ROSTER, manual(6), uuid4())
    assert (result.child_count, result.staff_count) == (6, 1)
    assert (result.required_staff, result.staff_deficit) == (2, 1)
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    assert composite.children.source is PresenceSource.MANUAL
    assert composite.qualified_staff.source is PresenceSource.STAFF_ROSTER
    assert composite.visitors.source is PresenceSource.MANUAL


def test_six_children_and_two_roster_staff_are_within_configured_policy() -> None:
    _, result = evaluate(ROSTER, manual(6), uuid4(), uuid4())
    assert result.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY
    assert (result.required_staff, result.staff_deficit) == (2, 0)


def test_no_roster_staff_is_zero_staff_and_over_ratio() -> None:
    composite, result = evaluate(ROSTER, manual(3))
    assert composite.qualified_staff.count == 0
    assert composite.qualified_staff.freshness is Freshness.FRESH
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    assert "NO_QUALIFIED_STAFF_PRESENT" in [str(item) for item in result.explanations]


def test_stale_or_missing_children_are_insufficient_whatever_the_roster_says() -> None:
    stale = manual(6, observed=NOW - timedelta(minutes=10))
    assert stale.availability is PresenceAvailability.PRESENCE_STALE
    _, result = evaluate(ROSTER, stale, uuid4(), uuid4())
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert result.child_count is None and result.staff_count is None
    missing = resolve_manual_presence([], ROOM_X, NOW)
    _, result = evaluate(ROSTER, missing, uuid4(), uuid4())
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert "CHILD_COUNT_MISSING" in [str(item) for item in result.explanations]
    _, result = evaluate(ROSTER, manual(6, revoked=True), uuid4(), uuid4())
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA


def test_manual_legacy_staff_is_never_added_to_roster_staff() -> None:
    # A report made before the switch still carries a staff number of 4.
    legacy = manual(6, staff=4)
    composite, result = evaluate(ROSTER, legacy, uuid4())
    assert result.staff_count == 1, "roster only: not 4, not 5"
    assert composite.qualified_staff.source is PresenceSource.STAFF_ROSTER
    _, manual_result = evaluate(PresenceSourceMode.MANUAL_AGGREGATE, legacy)
    assert manual_result.staff_count == 4


def test_manual_mode_is_exactly_v1_04b_and_ignores_the_roster() -> None:
    resolution = manual(6, staff=1)
    composite = compose_presence(
        ROOM_X,
        PresenceSourceMode.MANUAL_AGGREGATE,
        resolution,
        roster(uuid4(), uuid4()),
        NOW,
    )
    assert composite.snapshot == resolution.snapshot
    assert composite.roster is None
    assert evaluate_ratio(POLICY, composite.snapshot, NOW).staff_count == 1


def test_switching_back_to_manual_does_not_borrow_a_staff_number() -> None:
    roster_mode_report = manual(6, staff=None)
    _, result = evaluate(PresenceSourceMode.MANUAL_AGGREGATE, roster_mode_report)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert "STAFF_COUNT_MISSING" in [str(item) for item in result.explanations]


def test_roster_mode_requires_a_roster_for_this_classroom() -> None:
    with pytest.raises(RatioPolicyError, match="staff_roster_required"):
        compose_presence(ROOM_X, ROSTER, manual(6), None, NOW)
    other = resolve_staff_count(
        classroom_id=ROOM_Y, facility_id=FACILITY, now=NOW, members=[], events=[], assignments=[]
    )
    with pytest.raises(RatioPolicyError, match="staff_roster_required"):
        compose_presence(ROOM_X, ROSTER, manual(6), other, NOW)
    with pytest.raises(RatioPolicyError, match="invalid_presence_source_mode"):
        compose_presence(ROOM_X, "ROSTER", manual(6), None, NOW)  # type: ignore[arg-type]


def test_provenance_is_bounded_and_nameless() -> None:
    composite, _ = evaluate(ROSTER, manual(6, visitors=1), uuid4())
    body = composite.as_dict()
    assert body["mode"] == "ROSTER_STAFF_PLUS_MANUAL_CHILDREN"
    assert body["children"]["source"] == "MANUAL" and body["children"]["count"] == 6
    assert body["qualified_staff"]["source"] == "STAFF_ROSTER"
    assert body["qualified_staff"]["count"] == 1
    assert body["visitors"] == {
        "count": 1,
        "source": "MANUAL",
        "freshness": "FRESH",
        "valid_until": body["visitors"]["valid_until"],
    }
    assert set(body["staff_roster"]) == {
        "source",
        "count",
        "present",
        "present_ratio_ineligible",
        "present_inactive",
        "present_ambiguous",
        "stale",
        "freshness",
        "valid_until",
        "evaluated_at",
    }


# ============================================================================ role safety
def recognition_count(role: PresenceRole = PresenceRole.QUALIFIED_STAFF) -> PresenceCount:
    return PresenceCount(ROOM_X, role, 3, PresenceSource.STAFF_RECOGNITION, NOW, 60)


def test_a_face_recognition_count_is_never_authoritative_in_this_stage() -> None:
    with pytest.raises(RatioPolicyError, match="source_not_authoritative"):
        require_authoritative(recognition_count())
    # Smuggled in through a manual resolution's snapshot, it is still refused.
    snapshot = PresenceSnapshot(
        ROOM_X,
        children=PresenceCount(ROOM_X, PresenceRole.CHILD, 6, PresenceSource.MANUAL, NOW, 120),
        qualified_staff=recognition_count(),
    )
    smuggled = PresenceResolution(PresenceAvailability.PRESENCE_FRESH, None, snapshot)
    with pytest.raises(RatioPolicyError, match="source_not_authoritative"):
        compose_presence(ROOM_X, PresenceSourceMode.MANUAL_AGGREGATE, smuggled, None, NOW)


@pytest.mark.parametrize(
    "source",
    [
        PresenceSource.STAFF_RECOGNITION,
        PresenceSource.ATTENDANCE,
        PresenceSource.OTHER_APPROVED_SOURCE,
    ],
)
def test_only_manual_and_roster_sources_are_connected(source: PresenceSource) -> None:
    role = (
        PresenceRole.QUALIFIED_STAFF
        if source is PresenceSource.STAFF_RECOGNITION
        else PresenceRole.CHILD
    )
    with pytest.raises(RatioPolicyError, match="source_not_authoritative"):
        require_authoritative(PresenceCount(ROOM_X, role, 1, source, NOW, 60))


def test_unknown_visitor_and_vision_can_never_become_staff() -> None:
    # UNKNOWN and VISITOR counts cannot occupy the staff slot at all.
    for role in (PresenceRole.UNKNOWN, PresenceRole.VISITOR):
        with pytest.raises(RatioPolicyError):
            PresenceSnapshot(
                ROOM_X,
                qualified_staff=PresenceCount(ROOM_X, role, 2, PresenceSource.MANUAL, NOW, 60),
            )
    # The roster resolver has no input for a camera: a vision observation is not an event.
    with pytest.raises(TypeError):
        resolve_staff_count(  # type: ignore[call-arg]
            classroom_id=ROOM_X,
            facility_id=FACILITY,
            now=NOW,
            members=[],
            events=[],
            assignments=[],
            vision=VisionObservation(ROOM_X, 9, NOW),
        )
    # Only STAFF_ROSTER is ever produced by the resolver.
    resolution = roster(uuid4())
    assert resolution.to_presence_count().source is PresenceSource.STAFF_ROSTER


def test_a_visitor_count_never_changes_the_staff_count() -> None:
    staff = uuid4()
    _, without = evaluate(ROSTER, manual(6, visitors=0), staff)
    _, with_visitors = evaluate(ROSTER, manual(6, visitors=5), staff)
    assert without.staff_count == with_visitors.staff_count == 1
