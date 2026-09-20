"""Occupancy and coverage state derived from real tracker output.

Two rules govern everything here.

First, losing video is not evidence of an empty room. When coverage is not ACTIVE the
occupancy count is ``None`` and the certainty is UNKNOWN - never zero, never the last good
value quietly held over. A monitoring product that reports "0 people" when the camera died
is worse than one that reports nothing.

Second, nothing in this module classifies a person. There is no staff/child split, no age
estimate, no identity: the only quantity measured is how many distinct people the tracker is
currently confirming. Any staffing number comes from operator configuration, never from
looking at anybody.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from veotrex_edge_agent.tracking import TrackingFrameResult


class CoverageState(StrEnum):
    ACTIVE = "ACTIVE"
    IMPAIRED = "IMPAIRED"
    UNKNOWN = "UNKNOWN"


class OccupancyCertainty(StrEnum):
    MEASURED = "MEASURED"
    UNKNOWN = "UNKNOWN"


class ThresholdState(StrEnum):
    WITHIN_THRESHOLD = "WITHIN_THRESHOLD"
    OVER_THRESHOLD = "OVER_THRESHOLD"
    UNKNOWN = "UNKNOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class DemoStaffingPolicy:
    """An operator-declared demonstration threshold. Not a regulatory ratio.

    ``staff_on_duty`` is typed in by the operator; it is never derived from imagery. The
    product deliberately has no mechanism for telling staff from anyone else, so this
    compares a real head count against a declared capacity and nothing more.
    """

    staff_on_duty: int
    people_per_staff: int
    label: str = "Configured demo threshold"

    def __post_init__(self) -> None:
        if self.staff_on_duty < 1 or self.people_per_staff < 1:
            raise ValueError("demo_staffing_policy_must_be_positive")

    @property
    def permitted_people(self) -> int:
        return self.staff_on_duty * self.people_per_staff


@dataclass(frozen=True, slots=True)
class OccupancyReading:
    coverage: CoverageState
    certainty: OccupancyCertainty
    # None whenever certainty is UNKNOWN. Callers must render absence, not zero.
    people_detected: int | None
    confirmed_track_ids: tuple[int, ...]
    threshold_state: ThresholdState
    permitted_people: int | None
    observed_at_monotonic: float


def coverage_for(
    *,
    source_running: bool,
    seconds_since_last_frame: float | None,
    stale_after_seconds: float,
) -> CoverageState:
    """ACTIVE only while frames are genuinely arriving."""
    if not source_running:
        return CoverageState.UNKNOWN
    if seconds_since_last_frame is None:
        return CoverageState.UNKNOWN
    if seconds_since_last_frame > stale_after_seconds:
        return CoverageState.IMPAIRED
    return CoverageState.ACTIVE


def evaluate_threshold(
    people_detected: int | None, policy: DemoStaffingPolicy | None
) -> tuple[ThresholdState, int | None]:
    if policy is None:
        return ThresholdState.NOT_CONFIGURED, None
    if people_detected is None:
        return ThresholdState.UNKNOWN, policy.permitted_people
    if people_detected > policy.permitted_people:
        return ThresholdState.OVER_THRESHOLD, policy.permitted_people
    return ThresholdState.WITHIN_THRESHOLD, policy.permitted_people


def read_occupancy(
    tracking: TrackingFrameResult | None,
    *,
    coverage: CoverageState,
    observed_at_monotonic: float,
    policy: DemoStaffingPolicy | None = None,
) -> OccupancyReading:
    """Count confirmed tracks, or report that the count is unknown.

    Tentative tracks are excluded on purpose: they are the tracker's unconfirmed hypotheses,
    and counting them would make the headline number flicker on every noisy detection.
    """
    if coverage is not CoverageState.ACTIVE or tracking is None:
        threshold_state, permitted = evaluate_threshold(None, policy)
        return OccupancyReading(
            coverage=coverage,
            certainty=OccupancyCertainty.UNKNOWN,
            people_detected=None,
            confirmed_track_ids=(),
            threshold_state=threshold_state,
            permitted_people=permitted,
            observed_at_monotonic=observed_at_monotonic,
        )
    track_ids = tuple(sorted(track.track_id for track in tracking.confirmed_tracks))
    people = len(track_ids)
    threshold_state, permitted = evaluate_threshold(people, policy)
    return OccupancyReading(
        coverage=coverage,
        certainty=OccupancyCertainty.MEASURED,
        people_detected=people,
        confirmed_track_ids=track_ids,
        threshold_state=threshold_state,
        permitted_people=permitted,
        observed_at_monotonic=observed_at_monotonic,
    )
