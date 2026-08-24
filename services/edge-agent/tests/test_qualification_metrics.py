import pytest

from veotrex_edge_agent.qualification.metrics import (
    BoundedSamples,
    continuity_summary,
    decide,
    percentile,
)
from veotrex_edge_agent.qualification.models import (
    EngineeringTargets,
    QualificationDecision,
    SessionResult,
    TerminationReason,
)


def session(number: int, first: float | None, last: float | None) -> SessionResult:
    return SessionResult(
        session_number=number,
        requested_at=float(number),
        connection_started_at=float(number),
        first_media_at=first,
        last_media_at=last,
        ended_at=(last or float(number)) + 0.1,
        termination_reason=TerminationReason.PROVIDER_SESSION_EXPIRATION,
    )


def test_monotonic_duration_calculations() -> None:
    value = session(1, 11.0, 15.0)
    value.requested_at = 10.0
    value.connection_started_at = 10.5
    assert value.time_to_first_media_seconds == 1.0
    assert value.media_duration_seconds == 4.0
    assert value.observed_duration_seconds == 4.6


def test_transition_gaps_preserve_negative_overlap() -> None:
    summary = continuity_summary([session(1, 0.0, 10.0), session(2, 9.5, 20.0)])
    assert summary.transition_gaps_ms == (-500.0,)
    assert summary.total_blind_time_seconds == 0
    assert summary.media_availability_percent == 100


def test_availability_uses_positive_blind_time_only() -> None:
    summary = continuity_summary(
        [session(1, 0.0, 4.0), session(2, 5.0, 9.0), session(3, 8.5, 10.0)]
    )
    assert summary.total_blind_time_seconds == 1.0
    assert summary.observation_window_seconds == 10.0
    assert summary.media_availability_percent == 90.0


def test_percentiles_interpolate_deterministically() -> None:
    assert percentile([0.0, 10.0], 50) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == pytest.approx(3.85)
    assert percentile([], 99) is None


def test_bounded_samples_never_grow_past_capacity() -> None:
    values = BoundedSamples(capacity=3)
    for value in range(10):
        values.add(float(value))
    assert values.count == 10
    assert values.retained_count == 3
    assert values.values() == (7.0, 8.0, 9.0)
    assert values.minimum == 0.0
    assert values.maximum == 9.0


def test_decision_rubric_reports_insufficient_and_conditional() -> None:
    targets = EngineeringTargets()
    one = [session(1, 0.0, 10.0)]
    assert decide(continuity_summary(one), one, targets) is QualificationDecision.INSUFFICIENT_DATA
    two = [session(1, 0.0, 1000.0), session(2, 1001.0, 2000.0)]
    assert decide(continuity_summary(two), two, targets) is QualificationDecision.CONDITIONAL
