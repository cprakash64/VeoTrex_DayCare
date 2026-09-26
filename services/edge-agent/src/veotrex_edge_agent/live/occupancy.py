"""Occupancy evidence: which confirmed person tracks count toward the room's head count (V1-03B).

Four things are kept apart on purpose, because collapsing any two of them into one threshold is
how a system either invents occupants or loses real ones:

DETECTION
    The model emitted a person candidate for one frame.
TRACK
    The tracker associated detections over time (ADR 0012). It is deliberately high-recall: a
    low-score detection may keep an established track alive, so a person who is partly
    occluded or turned away is not dropped the moment their score dips.
OCCUPANCY ELIGIBILITY
    Whether the evidence *behind a confirmed track* is strong enough for it to change the head
    count. Decided here.
NUISANCE CALIBRATION
    An operator's explicit exclusion of a known fixed artifact (``recorded/regions.py``).
    Never applied from here.

**The rule.** A confirmed track is ``OCCUPANCY_CANDIDATE`` until at least
``min_high_observations`` of its last ``evidence_window`` observations were high-score
detections by the tracker's *own* high threshold; it is then ``OCCUPANCY_VALIDATED`` and stays
so until the track ends. No new confidence threshold is introduced, and nothing about motion is
consulted: a person asleep in a chair is validated exactly as fast as one walking, and a
moving low-confidence box is not validated for moving.

Why this and not a higher detector or tracker threshold: the one nuisance that motivated this
stage was a static box scoring 0.07-0.32, but children, distant people and partly occluded
people can legitimately score in that range too. Raising a global threshold would hide them
silently. Here a weak track is not hidden - it is shown as a *candidate*, counted separately,
and explained - so uncertainty is visible rather than resolved by pretending.

Validation is sticky for the life of the track. Once a person has been seen clearly, a later
stretch of weak detections (turning away, partial occlusion) must not make the count flap; the
track still ends through the tracker's normal time-based tolerance when they are really gone.

Candidates never produce appearance/disappearance events, never change occupancy, and must not
feed any compliance logic. A candidate is not "not a person", not a child, not anything: it is
a track whose evidence is not yet strong enough to count.

**Nuisance diagnostics** are bounded per-track statistics - geometry normalised to the frame and
confidence summaries, no pixels - kept so an operator can review a persistent candidate. A
long-lived candidate is labelled ``PERSISTENT_LOW_CONFIDENCE_CANDIDATE`` and given a *suggested*
ignore region. The suggestion is inert: it is printed for review and applied only if an operator
configures it. The label is a prompt to look, never a conclusion.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from veotrex_edge_agent.qualification.metrics import percentile
from veotrex_edge_agent.recorded.regions import (
    DEFAULT_MIN_CONTAINMENT,
    IgnoreRegion,
    IgnoreRegionError,
)
from veotrex_edge_agent.tracking import TrackingConfig

OCCUPANCY_VALIDATED = "OCCUPANCY_VALIDATED"
OCCUPANCY_CANDIDATE = "OCCUPANCY_CANDIDATE"
PERSISTENT_LOW_CONFIDENCE_CANDIDATE = "PERSISTENT_LOW_CONFIDENCE_CANDIDATE"

# Far above the tracker's own bounds (128 active + 128 lost): reaching it means a bookkeeping
# fault, and the ledger refuses new tracks rather than growing.
MAX_TRACKED_EVIDENCE = 512
# Diagnostics of recently ended tracks, kept so a candidate that came and went can be reviewed.
FINISHED_DIAGNOSTICS_CAPACITY = 16
# Recent scores kept per track for quantiles.
RECENT_SCORE_CAPACITY = 32


@dataclass(frozen=True, slots=True)
class OccupancyEvidencePolicy:
    """How much evidence a confirmed track needs before it counts. Validated and bounded.

    ``high_score_threshold`` is the tracker's own, never a separate value: "high-score" means
    the same thing to the tracker and to occupancy. The defaults are provisional, like the
    tracker's (ADR 0012), and are not a daycare or child qualification.
    """

    high_score_threshold: float = 0.30
    # 3 of the last 10: at the default 6 fps, two consecutive high observations confirm a track
    # (the tracker's rule) and one more independent high observation within ~1.7 s validates it.
    min_high_observations: int = 3
    evidence_window: int = 10
    # A candidate that has lived this long, over this many observations, is flagged for review.
    nuisance_min_seconds: float = 5.0
    nuisance_min_observations: int = 10
    # A suggested region is the track's box envelope grown by this fraction of its size.
    suggestion_margin: float = 0.10

    def __post_init__(self) -> None:
        if not 0.0 < self.high_score_threshold <= 1.0:
            raise ValueError("occupancy high score threshold must be in (0, 1]")
        if not 1 <= self.min_high_observations <= self.evidence_window <= 64:
            raise ValueError("occupancy evidence must satisfy 1 <= min high <= window <= 64")
        if not 0.0 < self.nuisance_min_seconds <= 3600.0:
            raise ValueError("nuisance minimum seconds must be in (0, 3600]")
        if not 1 <= self.nuisance_min_observations <= 100_000:
            raise ValueError("nuisance minimum observations must be positive")
        if not 0.0 <= self.suggestion_margin <= 0.5:
            raise ValueError("suggestion margin must be in [0, 0.5]")

    @classmethod
    def from_tracking(cls, config: TrackingConfig, **overrides: Any) -> OccupancyEvidencePolicy:
        return cls(high_score_threshold=config.high_score_threshold, **overrides)

    def as_dict(self) -> dict[str, Any]:
        return {
            "high_score_threshold": self.high_score_threshold,
            "min_high_observations": self.min_high_observations,
            "evidence_window": self.evidence_window,
            "nuisance_min_seconds": self.nuisance_min_seconds,
            "nuisance_min_observations": self.nuisance_min_observations,
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


@dataclass(slots=True)
class TrackEvidence:
    """Bounded evidence for one confirmed track: counters, extrema and running moments only."""

    track_id: int
    window: deque[bool]
    first_seen_ms: float | None = None
    last_seen_ms: float | None = None
    # Includes the pre-confirmation observations, which the tracker guarantees were high.
    observations: int = 0
    high_observations: int = 0
    # Observations whose score and box this ledger actually saw.
    scored_observations: int = 0
    confidence_min: float = math.inf
    confidence_max: float = -math.inf
    confidence_sum: float = 0.0
    recent_scores: deque[float] = field(default_factory=lambda: deque(maxlen=RECENT_SCORE_CAPACITY))
    # Normalised box envelope and running moments (Welford) of centre and size.
    envelope: list[float] = field(default_factory=lambda: [1.0, 1.0, 0.0, 0.0])
    centre_mean: list[float] = field(default_factory=lambda: [0.0, 0.0])
    centre_m2: list[float] = field(default_factory=lambda: [0.0, 0.0])
    size_sum: list[float] = field(default_factory=lambda: [0.0, 0.0])
    aspect_sum: float = 0.0
    status: str = OCCUPANCY_CANDIDATE
    validated_at_ms: float | None = None

    @property
    def validated(self) -> bool:
        return self.status == OCCUPANCY_VALIDATED

    @property
    def recent_high(self) -> int:
        return sum(self.window)

    def seed(self, prior_high_observations: int) -> None:
        """Observations made before confirmation, all high-score by the tracker's contract."""
        for _ in range(max(0, prior_high_observations)):
            self.observations += 1
            self.high_observations += 1
            self.window.append(True)

    def add(
        self,
        score: float,
        bbox: tuple[float, ...],
        *,
        width: int,
        height: int,
        timestamp_ms: float,
        high: bool,
    ) -> None:
        self.observations += 1
        self.high_observations += int(high)
        self.window.append(high)
        if self.first_seen_ms is None:
            self.first_seen_ms = timestamp_ms
        self.last_seen_ms = timestamp_ms
        if not math.isfinite(score) or width <= 0 or height <= 0 or len(bbox) < 4:
            return
        self.scored_observations += 1
        self.confidence_min = min(self.confidence_min, score)
        self.confidence_max = max(self.confidence_max, score)
        self.confidence_sum += score
        self.recent_scores.append(score)
        x1, y1, x2, y2 = (
            float(bbox[0]) / width,
            float(bbox[1]) / height,
            float(bbox[2]) / width,
            float(bbox[3]) / height,
        )
        envelope = self.envelope
        envelope[0], envelope[1] = min(envelope[0], x1), min(envelope[1], y1)
        envelope[2], envelope[3] = max(envelope[2], x2), max(envelope[3], y2)
        count = self.scored_observations
        for axis, value in enumerate(((x1 + x2) / 2.0, (y1 + y2) / 2.0)):
            delta = value - self.centre_mean[axis]
            self.centre_mean[axis] += delta / count
            self.centre_m2[axis] += delta * (value - self.centre_mean[axis])
        box_w, box_h = max(x2 - x1, 0.0), max(y2 - y1, 0.0)
        self.size_sum[0] += box_w
        self.size_sum[1] += box_h
        # Aspect in pixels, width over height, so it reads the way the box looks.
        pixel_h = box_h * height
        self.aspect_sum += (box_w * width / pixel_h) if pixel_h > 0 else 0.0

    def duration_ms(self) -> float:
        if self.first_seen_ms is None or self.last_seen_ms is None:
            return 0.0
        return self.last_seen_ms - self.first_seen_ms

    def labels(self, policy: OccupancyEvidencePolicy) -> list[str]:
        if (
            not self.validated
            and self.duration_ms() >= policy.nuisance_min_seconds * 1000.0
            and self.observations >= policy.nuisance_min_observations
        ):
            return [PERSISTENT_LOW_CONFIDENCE_CANDIDATE]
        return []

    def suggested_region(self, policy: OccupancyEvidencePolicy) -> dict[str, Any] | None:
        """The envelope grown by the margin, if it would be an acceptable region at all."""
        if self.scored_observations == 0:
            return None
        x1, y1, x2, y2 = self.envelope
        margin_x = (x2 - x1) * policy.suggestion_margin
        margin_y = (y2 - y1) * policy.suggestion_margin
        bounds = (
            round(max(0.0, x1 - margin_x), 3),
            round(max(0.0, y1 - margin_y), 3),
            round(min(1.0, x2 + margin_x), 3),
            round(min(1.0, y2 + margin_y), 3),
        )
        try:
            region = IgnoreRegion(*bounds, label=f"review track {self.track_id}")
        except IgnoreRegionError:
            # Too large (or degenerate) to be a safe mask. Say nothing rather than suggest
            # something the region rules themselves would refuse.
            return None
        return {
            **region.as_dict(),
            "min_containment": DEFAULT_MIN_CONTAINMENT,
            "cli": f"--ignore-region {bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]},<label>",
            "requires_operator_review": True,
            "applied": False,
        }

    def diagnostics(self, policy: OccupancyEvidencePolicy) -> dict[str, Any]:
        scored = self.scored_observations
        recent = list(self.recent_scores)
        labels = self.labels(policy)
        spread = [math.sqrt(self.centre_m2[axis] / scored) if scored else None for axis in (0, 1)]
        result: dict[str, Any] = {
            "track_id": self.track_id,
            "status": self.status,
            "labels": labels,
            "first_seen_ms": _round(self.first_seen_ms, 1),
            "last_seen_ms": _round(self.last_seen_ms, 1),
            "duration_seconds": round(self.duration_ms() / 1000.0, 2),
            "observations": self.observations,
            "high_observations": self.high_observations,
            "high_observations_in_window": self.recent_high,
            "validated_at_ms": _round(self.validated_at_ms, 1),
            "confidence": {
                "min": _round(self.confidence_min) if scored else None,
                "max": _round(self.confidence_max) if scored else None,
                "mean": _round(self.confidence_sum / scored) if scored else None,
                "recent_p50": _round(percentile(recent, 50)),
                "recent_p95": _round(percentile(recent, 95)),
            },
            "bbox_envelope_normalized": (
                [round(value, 4) for value in self.envelope] if scored else None
            ),
            # Diagnostic only. Near-zero spread describes a box that did not move; it does not
            # and must not mean "not a person" - people sit, sleep and stand still.
            "centre_normalized": {
                "mean_x": _round(self.centre_mean[0]) if scored else None,
                "mean_y": _round(self.centre_mean[1]) if scored else None,
                "spread_x": _round(spread[0], 5),
                "spread_y": _round(spread[1], 5),
            },
            "size_normalized": {
                "mean_w": _round(self.size_sum[0] / scored) if scored else None,
                "mean_h": _round(self.size_sum[1] / scored) if scored else None,
            },
            "aspect_w_over_h_mean": _round(self.aspect_sum / scored, 3) if scored else None,
        }
        if labels:
            result["suggested_ignore_region"] = self.suggested_region(policy)
        return result


@dataclass(slots=True)
class OccupancyMetrics:
    candidate_tracks_total: int = 0
    validations_total: int = 0
    candidates_ended_unvalidated_total: int = 0
    validated_tracks_ended_total: int = 0
    evidence_capacity_refusals_total: int = 0


class OccupancyLedger:
    """Evidence for every live confirmed track, and the head count it supports. Bounded.

    Driven by the runtime from the pipeline's own lifecycle: ``start`` on TRACK_STARTED,
    ``observe`` on every observation, ``end`` on the track's summary. The pipeline thread
    writes and the dashboard's HTTP threads read, so every public method holds one lock.
    """

    def __init__(self, policy: OccupancyEvidencePolicy | None = None) -> None:
        self.policy = policy or OccupancyEvidencePolicy()
        self._live: dict[int, TrackEvidence] = {}
        self._finished: deque[dict[str, Any]] = deque(maxlen=FINISHED_DIAGNOSTICS_CAPACITY)
        self.metrics = OccupancyMetrics()
        self._lock = threading.Lock()

    def _new(self, track_id: int) -> TrackEvidence:
        return TrackEvidence(track_id, deque(maxlen=self.policy.evidence_window))

    def start(self, track_id: int, *, prior_high_observations: int = 0) -> None:
        with self._lock:
            self._start(track_id, prior_high_observations)

    def _start(self, track_id: int, prior_high_observations: int) -> None:
        if track_id in self._live:
            return
        if len(self._live) >= MAX_TRACKED_EVIDENCE:
            self.metrics.evidence_capacity_refusals_total += 1
            return
        evidence = self._new(track_id)
        evidence.seed(prior_high_observations)
        self._live[track_id] = evidence
        self.metrics.candidate_tracks_total += 1

    def observe(
        self,
        track_id: int,
        *,
        score: float,
        bbox: tuple[float, ...],
        width: int,
        height: int,
        timestamp_ms: float,
    ) -> bool:
        """Record one matched detection. True exactly when this made the track count."""
        with self._lock:
            return self._observe(track_id, score, bbox, width, height, timestamp_ms)

    def _observe(
        self,
        track_id: int,
        score: float,
        bbox: tuple[float, ...],
        width: int,
        height: int,
        timestamp_ms: float,
    ) -> bool:
        evidence = self._live.get(track_id)
        if evidence is None:
            # The pipeline always starts a track before observing it; this is the defensive
            # path, and a track seen only here gets no pre-confirmation credit.
            self._start(track_id, 0)
            evidence = self._live.get(track_id)
            if evidence is None:
                return False
        high = math.isfinite(score) and score >= self.policy.high_score_threshold
        evidence.add(score, bbox, width=width, height=height, timestamp_ms=timestamp_ms, high=high)
        if not evidence.validated and evidence.recent_high >= self.policy.min_high_observations:
            evidence.status = OCCUPANCY_VALIDATED
            evidence.validated_at_ms = timestamp_ms
            self.metrics.validations_total += 1
            return True
        return False

    def end(self, track_id: int) -> TrackEvidence | None:
        with self._lock:
            evidence = self._live.pop(track_id, None)
            if evidence is None:
                return None
            if evidence.validated:
                self.metrics.validated_tracks_ended_total += 1
            else:
                self.metrics.candidates_ended_unvalidated_total += 1
            self._finished.append(evidence.diagnostics(self.policy))
            return evidence

    def statuses(self) -> dict[int, str]:
        with self._lock:
            return {track_id: evidence.status for track_id, evidence in self._live.items()}

    def status(self, track_id: int) -> str | None:
        with self._lock:
            evidence = self._live.get(track_id)
            return None if evidence is None else evidence.status

    @property
    def validated_count(self) -> int:
        with self._lock:
            return self._validated_count()

    def _validated_count(self) -> int:
        return sum(1 for evidence in self._live.values() if evidence.validated)

    @property
    def candidate_count(self) -> int:
        with self._lock:
            return len(self._live) - self._validated_count()

    def flagged_count(self) -> int:
        with self._lock:
            return self._flagged_count()

    def _flagged_count(self) -> int:
        return sum(1 for evidence in self._live.values() if evidence.labels(self.policy))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        metrics = self.metrics
        validated = self._validated_count()
        return {
            "occupancy_validated_tracks": validated,
            "occupancy_candidate_tracks": len(self._live) - validated,
            "occupancy_candidate_tracks_total": metrics.candidate_tracks_total,
            "occupancy_candidate_to_validated_total": metrics.validations_total,
            "occupancy_candidates_ended_unvalidated_total": (
                metrics.candidates_ended_unvalidated_total
            ),
            "occupancy_validated_tracks_ended_total": metrics.validated_tracks_ended_total,
            "occupancy_evidence_capacity_refusals_total": metrics.evidence_capacity_refusals_total,
            "nuisance_review_candidates": self._flagged_count(),
        }

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "policy": self.policy.as_dict(),
                "live": [
                    evidence.diagnostics(self.policy) for _, evidence in sorted(self._live.items())
                ],
                "recently_ended": list(self._finished),
            }
