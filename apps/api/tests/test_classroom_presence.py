"""Manual presence as the first authoritative source (V1-04B): the pure half.

Selection, freshness, revocation, role safety and the reconciliation boundary, with no database
and no clock of its own. Every count is a synthetic aggregate; nothing names or identifies a
person, and nothing comes from a camera except the diagnostic ``VisionObservation``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from veotrex_api.classroom_ratio import (
    MANUAL_DEFAULT_VALIDITY_SECONDS,
    MANUAL_MAX_CHILDREN,
    MANUAL_MAX_QUALIFIED_STAFF,
    MANUAL_MAX_VISITORS,
    Explanation,
    ManualPresenceRecord,
    PresenceAvailability,
    PresenceRole,
    PresenceSnapshot,
    PresenceSource,
    RatioPolicyError,
    RatioPolicyTerms,
    RatioState,
    ReconciliationState,
    VisionObservation,
    evaluate_ratio,
    reconcile_vision,
    resolve_manual_presence,
)

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
ROOM = UUID("00000000-0000-4000-8000-0000000000aa")
POLICY = RatioPolicyTerms(
    uuid4(),
    ROOM,
    "Configured classroom policy",
    5,
    0,
    None,
    NOW - timedelta(days=1),
    None,
    True,
    1,
)


def record(
    children: int = 6,
    staff: int = 1,
    visitors: int = 0,
    *,
    age: float = 10.0,
    valid_for: int = MANUAL_DEFAULT_VALIDITY_SECONDS,
    created_after: float = 0.0,
    revoked: bool = False,
    classroom: UUID = ROOM,
    snapshot_id: UUID | None = None,
) -> ManualPresenceRecord:
    observed = NOW - timedelta(seconds=age)
    return ManualPresenceRecord(
        snapshot_id=snapshot_id or uuid4(),
        classroom_id=classroom,
        child_count=children,
        qualified_staff_count=staff,
        visitor_count=visitors,
        observed_at=observed,
        valid_until=observed + timedelta(seconds=valid_for),
        created_at=observed + timedelta(seconds=created_after),
        revoked_at=NOW if revoked else None,
    )


def status_for(records: list[ManualPresenceRecord], at: datetime = NOW):  # type: ignore[no-untyped-def]
    resolution = resolve_manual_presence(records, ROOM, at)
    return resolution, evaluate_ratio(POLICY, resolution.snapshot, at)


# ================================================================================ records
@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"children": -1}, "invalid_child_count"),
        ({"staff": -1}, "invalid_qualified_staff_count"),
        ({"visitors": -1}, "invalid_visitor_count"),
        ({"children": MANUAL_MAX_CHILDREN + 1}, "invalid_child_count"),
        ({"staff": MANUAL_MAX_QUALIFIED_STAFF + 1}, "invalid_qualified_staff_count"),
        ({"visitors": MANUAL_MAX_VISITORS + 1}, "invalid_visitor_count"),
        ({"valid_for": 29}, "invalid_validity_seconds"),
        ({"valid_for": 15 * 60 + 1}, "invalid_validity_seconds"),
        ({"valid_for": 0}, "invalid_validity_seconds"),
        ({"valid_for": -60}, "invalid_validity_seconds"),
    ],
)
def test_out_of_bounds_reports_are_refused(changes: dict[str, int], category: str) -> None:
    with pytest.raises(RatioPolicyError, match=category):
        record(**changes)  # type: ignore[arg-type]


def test_booleans_are_not_counts() -> None:
    with pytest.raises(RatioPolicyError):
        record(children=True)  # bool is an int to the type checker; refused here


def test_a_manual_record_fills_exactly_the_child_staff_and_visitor_slots() -> None:
    snapshot = record(6, 1, 2).to_snapshot()
    assert snapshot.children is not None and snapshot.children.role is PresenceRole.CHILD
    assert snapshot.children.count == 6
    assert snapshot.qualified_staff is not None
    assert snapshot.qualified_staff.role is PresenceRole.QUALIFIED_STAFF
    assert snapshot.visitors is not None and snapshot.visitors.count == 2
    assert snapshot.unknown is None, "a manual report never invents an UNKNOWN count"
    assert {count.source for count in (snapshot.children, snapshot.qualified_staff)} == {
        PresenceSource.MANUAL
    }


def test_unknown_can_substitute_for_neither_child_nor_staff() -> None:
    base = record().to_snapshot()
    unknown = dataclasses.replace(base.children, role=PresenceRole.UNKNOWN)  # type: ignore[type-var]
    with pytest.raises(RatioPolicyError, match="presence_role_mismatch"):
        dataclasses.replace(base, children=unknown)
    with pytest.raises(RatioPolicyError, match="presence_role_mismatch"):
        dataclasses.replace(base, qualified_staff=unknown)


def test_visitors_count_as_neither_children_nor_staff() -> None:
    _, evaluation = status_for([record(6, 1, visitors=40)])
    assert (evaluation.child_count, evaluation.staff_count) == (6, 1)
    assert evaluation.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    visitors = record(6, 1, 3).to_snapshot().visitors
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, children=visitors)
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, qualified_staff=visitors)


def test_vision_can_populate_neither_count() -> None:
    observed = VisionObservation(ROOM, 8, NOW)
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, children=observed)  # type: ignore[arg-type]
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, qualified_staff=observed)  # type: ignore[arg-type]


def test_the_record_carries_no_person_image_or_track_fields() -> None:
    names = {field.name for field in dataclasses.fields(ManualPresenceRecord)}
    for forbidden in (
        "name",
        "face",
        "embedding",
        "image",
        "photo",
        "track",
        "box",
        "child_id",
        "staff_id",
        "person",
    ):
        assert not any(forbidden in name for name in names), forbidden


# ============================================================================== selection
def test_no_report_is_not_connected_and_insufficient() -> None:
    resolution, evaluation = status_for([])
    assert resolution.availability is PresenceAvailability.PRESENCE_NOT_CONNECTED
    assert not resolution.connected
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA


def test_a_fresh_report_is_selected() -> None:
    fresh = record()
    resolution, evaluation = status_for([fresh])
    assert resolution.availability is PresenceAvailability.PRESENCE_FRESH
    assert resolution.record == fresh
    assert evaluation.ratio_state is RatioState.OVER_CONFIGURED_RATIO


def test_a_stale_report_is_insufficient_and_says_stale() -> None:
    stale = record(age=MANUAL_DEFAULT_VALIDITY_SECONDS)  # expires exactly now
    resolution, evaluation = status_for([stale])
    assert resolution.availability is PresenceAvailability.PRESENCE_STALE
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA
    assert Explanation.CHILD_COUNT_STALE in evaluation.explanations
    assert evaluation.child_count is None and evaluation.required_staff is None


def test_a_revoked_report_is_not_authoritative() -> None:
    resolution, evaluation = status_for([record(revoked=True)])
    assert resolution.availability is PresenceAvailability.PRESENCE_REVOKED
    assert resolution.snapshot is None
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA
    assert Explanation.CHILD_COUNT_MISSING in evaluation.explanations


def test_a_newer_report_supersedes_an_older_one() -> None:
    old = record(5, 1, age=60)
    new = record(6, 1, age=5)
    for order in ([old, new], [new, old]):
        resolution, evaluation = status_for(order)
        assert resolution.record == new
        assert evaluation.child_count == 6


def test_revoking_the_latest_never_resurrects_an_earlier_report() -> None:
    older_but_fresh = record(5, 1, age=60)
    latest_revoked = record(6, 1, age=5, revoked=True)
    resolution, evaluation = status_for([older_but_fresh, latest_revoked])
    assert resolution.availability is PresenceAvailability.PRESENCE_REVOKED
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA


def test_timestamp_ties_resolve_deterministically() -> None:
    first = record(5, 1, snapshot_id=UUID("00000000-0000-4000-8000-000000000001"))
    second = record(6, 1, snapshot_id=UUID("00000000-0000-4000-8000-000000000002"))
    for order in ([first, second], [second, first]):
        assert resolve_manual_presence(order, ROOM, NOW).record == second, "id DESC breaks ties"
    later_created = record(4, 1, created_after=1, snapshot_id=UUID(int=1))
    assert resolve_manual_presence([second, later_created], ROOM, NOW).record == later_created


def test_another_classrooms_report_is_never_selected() -> None:
    elsewhere = record(classroom=uuid4(), age=1)
    resolution, _ = status_for([elsewhere])
    assert resolution.availability is PresenceAvailability.PRESENCE_NOT_CONNECTED


def test_a_report_from_beyond_the_skew_is_not_yet_valid() -> None:
    future = record(age=-600)
    resolution, evaluation = status_for([future])
    assert resolution.availability is PresenceAvailability.PRESENCE_NOT_YET_VALID
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA


# ================================================================================== ratio
@pytest.mark.parametrize(
    ("children", "staff", "state", "required", "deficit"),
    [
        (5, 1, RatioState.WITHIN_CONFIGURED_POLICY, 1, 0),
        (6, 1, RatioState.OVER_CONFIGURED_RATIO, 2, 1),
        (6, 2, RatioState.WITHIN_CONFIGURED_POLICY, 2, 0),
        (1, 0, RatioState.OVER_CONFIGURED_RATIO, 1, 1),
        (0, 0, RatioState.NO_CHILDREN_PRESENT, 0, 0),
    ],
)
def test_manual_counts_through_the_unchanged_engine(
    children: int, staff: int, state: RatioState, required: int, deficit: int
) -> None:
    _, evaluation = status_for([record(children, staff)])
    assert evaluation.ratio_state is state
    assert (evaluation.required_staff, evaluation.staff_deficit) == (required, deficit)


# ======================================================================== reconciliation
def test_the_extra_person_the_camera_sees_is_unexplained_not_a_child() -> None:
    report = record(6, 1, 0)
    resolution, evaluation = status_for([report])
    reconciliation = reconcile_vision(resolution.snapshot, VisionObservation(ROOM, 8, NOW), NOW)
    assert reconciliation.state is ReconciliationState.VISION_HIGHER_THAN_ROSTER
    assert reconciliation.unexplained_observed_people == 1
    assert (evaluation.child_count, evaluation.staff_count) == (6, 1)
    assert report.child_count == 6, "the stored report is never touched"


# ============================================================================ scenarios
def scenario(children: int, staff: int, visitors: int, vision: int | None = None, **kwargs):  # type: ignore[no-untyped-def]
    resolution, evaluation = status_for([record(children, staff, visitors, **kwargs)])
    observed = None if vision is None else VisionObservation(ROOM, vision, NOW)
    fresh = (
        resolution.snapshot
        if resolution.availability is PresenceAvailability.PRESENCE_FRESH
        else None
    )
    return evaluation, reconcile_vision(fresh, observed, NOW)


def test_scenario_a() -> None:
    evaluation, _ = scenario(5, 1, 0)
    assert evaluation.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY


def test_scenario_b() -> None:
    evaluation, _ = scenario(6, 1, 0)
    assert evaluation.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    assert (evaluation.required_staff, evaluation.staff_deficit) == (2, 1)


def test_scenario_c() -> None:
    evaluation, _ = scenario(6, 2, 0)
    assert evaluation.ratio_state is RatioState.WITHIN_CONFIGURED_POLICY


def test_scenario_d_a_reported_visitor_explains_the_eighth_person() -> None:
    evaluation, reconciliation = scenario(6, 1, 1, vision=8)
    assert (evaluation.child_count, evaluation.staff_count) == (6, 1)
    assert reconciliation.state is ReconciliationState.AGREES
    assert reconciliation.authoritative_expected_people == 8


def test_scenario_e_an_unreported_eighth_person_is_unexplained() -> None:
    evaluation, reconciliation = scenario(6, 1, 0, vision=8)
    assert (evaluation.child_count, evaluation.staff_count) == (6, 1)
    assert reconciliation.state is ReconciliationState.VISION_HIGHER_THAN_ROSTER
    assert reconciliation.unexplained_observed_people == 1


def test_scenario_f_expiry_carries_nothing_forward() -> None:
    report = record(6, 1, 0, age=10)
    _, before = status_for([report])
    assert before.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    _, after = status_for([report], NOW + timedelta(seconds=MANUAL_DEFAULT_VALIDITY_SECONDS))
    assert after.ratio_state is RatioState.INSUFFICIENT_DATA
    assert after.required_staff is None and after.staff_deficit is None


def test_scenario_g_revoked_is_insufficient() -> None:
    evaluation, reconciliation = scenario(6, 1, 0, vision=7, revoked=True)
    assert evaluation.ratio_state is RatioState.INSUFFICIENT_DATA
    assert reconciliation.state is ReconciliationState.NOT_AVAILABLE
