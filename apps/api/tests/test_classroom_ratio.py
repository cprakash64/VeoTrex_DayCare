"""The pure classroom ratio engine (V1-04A): arithmetic, freshness, roles and reconciliation.

No database, no clock, no camera. Every count is synthetic and every role comes from an
explicitly approved source; the tests that matter most prove what the engine *refuses* to do:
turn an UNKNOWN person into a child or staff, take a child count from a camera, or keep
trusting a count that has gone stale.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from veotrex_api import classroom_ratio
from veotrex_api.classroom_ratio import (
    Explanation,
    Freshness,
    PresenceCount,
    PresenceRole,
    PresenceSnapshot,
    PresenceSource,
    RatioPolicyError,
    RatioPolicyTerms,
    RatioState,
    ReconciliationState,
    VisionObservation,
    evaluate_ratio,
    find_overlap,
    reconcile_vision,
    required_staff,
    select_policy,
)

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
ROOM = UUID("00000000-0000-4000-8000-000000000001")


def policy(
    ratio: int = 5,
    *,
    minimum: int = 0,
    group: int | None = None,
    starts: datetime = NOW - timedelta(days=1),
    ends: datetime | None = None,
    active: bool = True,
    revision: int = 1,
    classroom: UUID = ROOM,
    policy_id: UUID | None = None,
) -> RatioPolicyTerms:
    return RatioPolicyTerms(
        policy_id=policy_id or uuid4(),
        classroom_id=classroom,
        label="Configured classroom policy",
        max_children_per_staff=ratio,
        minimum_staff=minimum,
        maximum_group_size=group,
        effective_from=starts,
        effective_until=ends,
        active=active,
        revision=revision,
    )


def count(
    role: PresenceRole,
    value: int,
    *,
    source: PresenceSource = PresenceSource.MANUAL,
    age: timedelta = timedelta(seconds=10),
    valid_for: int = 300,
    classroom: UUID = ROOM,
) -> PresenceCount:
    return PresenceCount(classroom, role, value, source, NOW - age, valid_for)


def presence(
    children: int | None, staff: int | None, *, visitors: int | None = None, **kwargs: object
) -> PresenceSnapshot:
    return PresenceSnapshot(
        ROOM,
        children=None if children is None else count(PresenceRole.CHILD, children, **kwargs),  # type: ignore[arg-type]
        qualified_staff=None
        if staff is None
        else count(PresenceRole.QUALIFIED_STAFF, staff, **kwargs),  # type: ignore[arg-type]
        visitors=None if visitors is None else count(PresenceRole.VISITOR, visitors, **kwargs),  # type: ignore[arg-type]
    )


def vision(people: int, *, age: timedelta = timedelta(seconds=2)) -> VisionObservation:
    return VisionObservation(ROOM, people, NOW - age)


# ================================================================================ policies
def test_a_valid_policy_is_accepted() -> None:
    terms = policy(5, minimum=1, group=12)
    assert terms.applies_at(NOW)


@pytest.mark.parametrize("ratio", [0, -1, 101])
def test_a_non_positive_or_absurd_ratio_is_rejected(ratio: int) -> None:
    with pytest.raises(RatioPolicyError, match="invalid_max_children_per_staff"):
        policy(ratio)


def test_a_negative_minimum_staff_is_rejected() -> None:
    with pytest.raises(RatioPolicyError, match="invalid_minimum_staff"):
        policy(5, minimum=-1)


@pytest.mark.parametrize("group", [0, -3, 501])
def test_an_invalid_group_size_is_rejected(group: int) -> None:
    with pytest.raises(RatioPolicyError, match="invalid_maximum_group_size"):
        policy(5, group=group)


def test_booleans_and_floats_are_not_integers_here() -> None:
    with pytest.raises(RatioPolicyError):
        policy(True)  # bool is an int to the type checker; the engine refuses it
    with pytest.raises(RatioPolicyError):
        policy(5.0)  # type: ignore[arg-type]


def test_an_inverted_or_empty_effective_period_is_rejected() -> None:
    with pytest.raises(RatioPolicyError, match="effective_period_inverted"):
        policy(starts=NOW, ends=NOW - timedelta(hours=1))
    with pytest.raises(RatioPolicyError, match="effective_period_inverted"):
        policy(starts=NOW, ends=NOW)


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(RatioPolicyError):
        policy(starts=datetime(2026, 9, 1))  # naive on purpose: must be refused


def test_an_inactive_policy_is_never_selected() -> None:
    assert select_policy([policy(active=False)], NOW).policy is None


def test_a_future_policy_is_not_active_early() -> None:
    future = policy(starts=NOW + timedelta(minutes=1))
    assert select_policy([future], NOW).policy is None
    assert select_policy([future], NOW + timedelta(minutes=1)).policy == future


def test_an_expired_policy_is_not_active() -> None:
    expired = policy(starts=NOW - timedelta(days=10), ends=NOW)
    assert select_policy([expired], NOW).policy is None, "the end instant is exclusive"
    assert select_policy([expired], NOW - timedelta(seconds=1)).policy == expired


def test_overlapping_active_policies_are_detected_but_adjacent_ones_are_not() -> None:
    first = policy(starts=NOW - timedelta(days=10), ends=NOW)
    adjacent = policy(starts=NOW)
    overlapping = policy(starts=NOW - timedelta(days=1))
    assert find_overlap(adjacent, [first]) is None
    assert find_overlap(overlapping, [first]) == first
    assert find_overlap(overlapping, [dataclasses.replace(first, active=False)]) is None
    other_room = policy(starts=NOW - timedelta(days=1), classroom=uuid4())
    assert find_overlap(other_room, [first]) is None


def test_ambiguous_selection_is_deterministic_and_reported() -> None:
    older = policy(4, starts=NOW - timedelta(days=5), revision=9)
    newer = policy(6, starts=NOW - timedelta(days=1), revision=1)
    for order in ([older, newer], [newer, older]):
        selection = select_policy(order, NOW)
        assert selection.policy == newer and selection.ambiguous
    evaluation = evaluate_ratio(newer, presence(3, 1), NOW, policy_ambiguous=True)
    assert Explanation.POLICY_SELECTION_AMBIGUOUS in evaluation.explanations


# ============================================================================ ratio engine
@pytest.mark.parametrize(
    ("children", "staff", "ratio", "required", "deficit", "state"),
    [
        (0, 0, 5, 0, 0, RatioState.NO_CHILDREN_PRESENT),
        (1, 1, 5, 1, 0, RatioState.WITHIN_CONFIGURED_POLICY),
        (5, 1, 5, 1, 0, RatioState.WITHIN_CONFIGURED_POLICY),
        (6, 1, 5, 2, 1, RatioState.OVER_CONFIGURED_RATIO),
        (10, 2, 5, 2, 0, RatioState.WITHIN_CONFIGURED_POLICY),
        (11, 2, 5, 3, 1, RatioState.OVER_CONFIGURED_RATIO),
        (3, 0, 5, 1, 1, RatioState.OVER_CONFIGURED_RATIO),
    ],
)
def test_ratio_arithmetic(
    children: int, staff: int, ratio: int, required: int, deficit: int, state: RatioState
) -> None:
    result = evaluate_ratio(policy(ratio), presence(children, staff), NOW)
    assert result.ratio_state is state
    assert result.required_staff == required
    assert result.staff_deficit == deficit
    assert (result.child_count, result.staff_count) == (children, staff)


def test_children_with_no_staff_is_stated_plainly() -> None:
    result = evaluate_ratio(policy(5), presence(3, 0), NOW)
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    assert Explanation.NO_QUALIFIED_STAFF_PRESENT in result.explanations


def test_no_children_is_never_a_violation_even_with_no_staff() -> None:
    result = evaluate_ratio(policy(5, minimum=2), presence(0, 0), NOW)
    assert result.ratio_state is RatioState.NO_CHILDREN_PRESENT
    assert result.conditions == ()
    assert result.required_staff == 0


def test_minimum_staff_raises_the_requirement() -> None:
    assert required_staff(3, 5, 2) == 2
    result = evaluate_ratio(policy(5, minimum=2), presence(3, 1), NOW)
    assert result.required_staff == 2 and result.staff_deficit == 1
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO


def test_group_size_is_represented_independently_of_the_ratio() -> None:
    only_group = evaluate_ratio(policy(5, group=8), presence(9, 2), NOW)
    assert only_group.ratio_state is RatioState.OVER_CONFIGURED_GROUP_SIZE
    assert only_group.conditions == (RatioState.OVER_CONFIGURED_GROUP_SIZE,)
    both = evaluate_ratio(policy(5, group=8), presence(9, 1), NOW)
    assert both.conditions == (
        RatioState.OVER_CONFIGURED_RATIO,
        RatioState.OVER_CONFIGURED_GROUP_SIZE,
    ), "neither condition may hide the other"
    assert both.group_size == 9 and both.maximum_group_size == 8


def test_no_policy_is_not_configured() -> None:
    result = evaluate_ratio(None, presence(6, 1), NOW)
    assert result.ratio_state is RatioState.NOT_CONFIGURED
    assert result.required_staff is None


def test_an_inactive_classroom_is_not_evaluated() -> None:
    result = evaluate_ratio(policy(5), presence(6, 1), NOW, classroom_active=False)
    assert result.ratio_state is RatioState.NOT_CONFIGURED
    assert result.explanations == (Explanation.CLASSROOM_INACTIVE,)


@pytest.mark.parametrize(
    ("snapshot", "explanation"),
    [
        (
            PresenceSnapshot(ROOM, qualified_staff=count(PresenceRole.QUALIFIED_STAFF, 1)),
            Explanation.CHILD_COUNT_MISSING,
        ),
        (
            PresenceSnapshot(ROOM, children=count(PresenceRole.CHILD, 3)),
            Explanation.STAFF_COUNT_MISSING,
        ),
        (
            PresenceSnapshot(
                ROOM,
                children=count(PresenceRole.CHILD, 3, age=timedelta(minutes=10)),
                qualified_staff=count(PresenceRole.QUALIFIED_STAFF, 1),
            ),
            Explanation.CHILD_COUNT_STALE,
        ),
        (
            PresenceSnapshot(
                ROOM,
                children=count(PresenceRole.CHILD, 3),
                qualified_staff=count(PresenceRole.QUALIFIED_STAFF, 1, age=timedelta(minutes=10)),
            ),
            Explanation.STAFF_COUNT_STALE,
        ),
    ],
)
def test_missing_or_stale_counts_are_insufficient_data(
    snapshot: PresenceSnapshot, explanation: Explanation
) -> None:
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert explanation in result.explanations
    assert result.required_staff is None and result.staff_deficit is None
    assert result.child_count is None and result.staff_count is None


def test_nothing_is_carried_forward_when_a_count_goes_stale() -> None:
    """Neither the last safe state nor the last violation survives expiry."""
    snapshot = presence(6, 1, valid_for=60)
    violating = evaluate_ratio(policy(5), snapshot, NOW)
    assert violating.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    later = evaluate_ratio(policy(5), snapshot, NOW + timedelta(seconds=60))
    assert later.ratio_state is RatioState.INSUFFICIENT_DATA
    safe = presence(5, 1, valid_for=60)
    assert evaluate_ratio(policy(5), safe, NOW + timedelta(seconds=60)).ratio_state is (
        RatioState.INSUFFICIENT_DATA
    )


def test_a_count_from_the_future_is_not_trusted() -> None:
    future = count(PresenceRole.CHILD, 3, age=-timedelta(minutes=10))
    assert classroom_ratio.freshness(future, NOW) is Freshness.STALE


def test_presence_counts_are_bounded() -> None:
    with pytest.raises(RatioPolicyError):
        count(PresenceRole.CHILD, -1)
    with pytest.raises(RatioPolicyError):
        count(PresenceRole.CHILD, 501)
    with pytest.raises(RatioPolicyError):
        count(PresenceRole.CHILD, 3, valid_for=0)
    with pytest.raises(RatioPolicyError):
        count(PresenceRole.CHILD, 3, valid_for=12 * 3600 + 1)


# ============================================================================= role safety
def test_unknown_is_never_a_child() -> None:
    unknown = count(PresenceRole.UNKNOWN, 7)
    with pytest.raises(RatioPolicyError, match="presence_role_mismatch"):
        PresenceSnapshot(ROOM, children=unknown)
    snapshot = PresenceSnapshot(
        ROOM, unknown=unknown, qualified_staff=count(PresenceRole.QUALIFIED_STAFF, 1)
    )
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert Explanation.CHILD_COUNT_MISSING in result.explanations


def test_unknown_is_never_qualified_staff() -> None:
    with pytest.raises(RatioPolicyError, match="presence_role_mismatch"):
        PresenceSnapshot(ROOM, qualified_staff=count(PresenceRole.UNKNOWN, 1))
    snapshot = PresenceSnapshot(
        ROOM, children=count(PresenceRole.CHILD, 3), unknown=count(PresenceRole.UNKNOWN, 2)
    )
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert Explanation.STAFF_COUNT_MISSING in result.explanations


def test_a_visitor_is_neither_a_child_nor_qualified_staff() -> None:
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, children=count(PresenceRole.VISITOR, 1))
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, qualified_staff=count(PresenceRole.VISITOR, 1))
    result = evaluate_ratio(policy(5), presence(6, 1, visitors=4), NOW)
    assert (result.child_count, result.staff_count) == (6, 1)
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO


def test_vision_occupancy_cannot_supply_a_child_or_staff_count() -> None:
    observed = vision(8)
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, children=observed)  # type: ignore[arg-type]
    with pytest.raises(RatioPolicyError):
        PresenceSnapshot(ROOM, qualified_staff=observed)  # type: ignore[arg-type]
    with pytest.raises(RatioPolicyError):
        evaluate_ratio(policy(5), observed, NOW)  # type: ignore[arg-type]


def test_there_is_no_vision_or_detector_presence_source() -> None:
    names = {source.name for source in PresenceSource}
    assert not {name for name in names if "VISION" in name or "DETECT" in name or "CAMERA" in name}
    with pytest.raises(RatioPolicyError, match="unapproved_presence_source"):
        PresenceCount(ROOM, PresenceRole.CHILD, 3, "PERSON_DETECTOR", NOW, 60)  # type: ignore[arg-type]


def test_staff_recognition_can_never_declare_a_child() -> None:
    with pytest.raises(RatioPolicyError, match="source_cannot_assert_role"):
        count(PresenceRole.CHILD, 1, source=PresenceSource.STAFF_RECOGNITION)
    with pytest.raises(RatioPolicyError, match="source_cannot_assert_role"):
        count(PresenceRole.CHILD, 1, source=PresenceSource.STAFF_ROSTER)
    with pytest.raises(RatioPolicyError, match="source_cannot_assert_role"):
        count(PresenceRole.QUALIFIED_STAFF, 1, source=PresenceSource.ATTENDANCE)
    assert count(PresenceRole.QUALIFIED_STAFF, 1, source=PresenceSource.STAFF_RECOGNITION)


def test_no_code_derives_children_from_observed_people_minus_staff() -> None:
    """Asserted against the source: no subtraction involving an observed-people quantity may
    produce a child count anywhere in the ratio domain or its service."""
    root = Path(classroom_ratio.__file__).parent
    for name in ("classroom_ratio.py", "classroom_service.py", "classroom_api.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_text = " ".join(ast.unparse(target) for target in targets).lower()
            if "child" not in target_text:
                continue
            value = ast.unparse(node.value) if node.value is not None else ""
            assert not ("observed" in value.lower() and "-" in value), (name, target_text, value)


def test_latest_snapshot_ignores_other_classrooms_and_keeps_roles_apart() -> None:
    other = uuid4()
    counts = [
        count(PresenceRole.CHILD, 3, age=timedelta(seconds=30)),
        count(PresenceRole.CHILD, 4, age=timedelta(seconds=5)),
        count(PresenceRole.CHILD, 9, classroom=other),
        count(PresenceRole.UNKNOWN, 2),
    ]
    snapshot = PresenceSnapshot.latest(ROOM, counts)
    assert snapshot.children is not None and snapshot.children.count == 4
    assert snapshot.unknown is not None and snapshot.unknown.count == 2
    assert snapshot.qualified_staff is None


# ======================================================================== reconciliation
def test_reconciliation_agrees() -> None:
    result = reconcile_vision(presence(5, 1), vision(6), NOW)
    assert result.state is ReconciliationState.AGREES
    assert result.unexplained_observed_people == 0


def test_vision_higher_is_unexplained_people_not_children() -> None:
    snapshot = presence(6, 1)
    result = reconcile_vision(snapshot, vision(8), NOW)
    assert result.state is ReconciliationState.VISION_HIGHER_THAN_ROSTER
    assert result.unexplained_observed_people == 1
    assert result.authoritative_expected_people == 7
    # The ratio still rests on the authoritative 6/1, untouched by what the camera saw.
    evaluation = evaluate_ratio(policy(5), snapshot, NOW)
    assert (evaluation.child_count, evaluation.staff_count) == (6, 1)
    assert snapshot.children is not None and snapshot.children.count == 6


def test_vision_lower_than_roster() -> None:
    result = reconcile_vision(presence(6, 1), vision(5), NOW)
    assert result.state is ReconciliationState.VISION_LOWER_THAN_ROSTER
    assert result.unseen_expected_people == 2 and result.unexplained_observed_people == 0


def test_reconciliation_counts_visitors_only_when_supplied() -> None:
    with_visitor = reconcile_vision(presence(5, 1, visitors=1), vision(7), NOW)
    assert with_visitor.state is ReconciliationState.AGREES and with_visitor.visitors_included
    without = reconcile_vision(presence(5, 1), vision(7), NOW)
    assert without.state is ReconciliationState.VISION_HIGHER_THAN_ROSTER
    assert "VISITOR_COUNT_NOT_SUPPLIED" in without.reasons


@pytest.mark.parametrize(
    ("snapshot", "observed", "reason"),
    [
        (presence(5, 1), None, "VISION_NOT_CONNECTED"),
        (presence(5, 1), "stale", "VISION_STALE"),
        (None, 6, "PRESENCE_NOT_CONNECTED"),
        (presence(5, None), 6, "STAFF_COUNT_NOT_FRESH"),
    ],
)
def test_reconciliation_unavailable(
    snapshot: PresenceSnapshot | None, observed: object, reason: str
) -> None:
    seen = (
        None
        if observed is None
        else vision(6, age=timedelta(minutes=5))
        if observed == "stale"
        else vision(int(observed))  # type: ignore[call-overload]
    )
    result = reconcile_vision(snapshot, seen, NOW)
    assert result.state is ReconciliationState.NOT_AVAILABLE
    assert reason in result.reasons


def test_reconciliation_never_mutates_its_inputs() -> None:
    snapshot = presence(6, 1)
    observed = vision(8)
    before = (dataclasses.asdict(snapshot), dataclasses.asdict(observed))
    reconcile_vision(snapshot, observed, NOW)
    assert (dataclasses.asdict(snapshot), dataclasses.asdict(observed)) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.children = None  # type: ignore[misc]


# ======================================================================== scenarios A - E
def test_scenario_a_within_policy_and_reconciliation_agrees() -> None:
    snapshot = presence(5, 1)
    assert evaluate_ratio(policy(5), snapshot, NOW).ratio_state is (
        RatioState.WITHIN_CONFIGURED_POLICY
    )
    assert reconcile_vision(snapshot, vision(6), NOW).state is ReconciliationState.AGREES


def test_scenario_b_over_ratio_one_short_and_reconciliation_agrees() -> None:
    snapshot = presence(6, 1)
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    assert (result.required_staff, result.staff_deficit) == (2, 1)
    assert reconcile_vision(snapshot, vision(7), NOW).state is ReconciliationState.AGREES


def test_scenario_c_two_staff_is_within_policy() -> None:
    snapshot = presence(6, 2)
    assert evaluate_ratio(policy(5), snapshot, NOW).ratio_state is (
        RatioState.WITHIN_CONFIGURED_POLICY
    )
    assert reconcile_vision(snapshot, vision(8), NOW).state is ReconciliationState.AGREES


def test_scenario_d_missing_staff_is_insufficient_and_vision_is_not_used() -> None:
    snapshot = PresenceSnapshot(
        ROOM,
        children=count(PresenceRole.CHILD, 6),
        qualified_staff=count(PresenceRole.QUALIFIED_STAFF, 1, age=timedelta(hours=1)),
    )
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert result.ratio_state is RatioState.INSUFFICIENT_DATA
    assert result.staff_count is None and result.child_count is None
    reconciliation = reconcile_vision(snapshot, vision(7), NOW)
    assert reconciliation.state is ReconciliationState.NOT_AVAILABLE


def test_scenario_e_extra_person_is_unexplained_not_a_child() -> None:
    snapshot = presence(6, 1)
    result = evaluate_ratio(policy(5), snapshot, NOW)
    assert (result.child_count, result.staff_count) == (6, 1)
    assert result.ratio_state is RatioState.OVER_CONFIGURED_RATIO
    reconciliation = reconcile_vision(snapshot, vision(8), NOW)
    assert reconciliation.state is ReconciliationState.VISION_HIGHER_THAN_ROSTER
    assert reconciliation.unexplained_observed_people == 1
    assert evaluate_ratio(policy(5), snapshot, NOW).child_count == 6


# ================================================================================= privacy
def test_the_ratio_domain_holds_no_names_faces_or_identities() -> None:
    fields = set()
    for model in (
        PresenceCount,
        PresenceSnapshot,
        VisionObservation,
        RatioPolicyTerms,
        classroom_ratio.RatioEvaluation,
        classroom_ratio.VisionReconciliation,
    ):
        fields |= {field.name.lower() for field in dataclasses.fields(model)}
    for forbidden in (
        "name",
        "face",
        "embedding",
        "image",
        "photo",
        "person_id",
        "track_id",
        "birth",
        "age_years",
        "guardian",
    ):
        assert not any(forbidden in field for field in fields), forbidden
