"""The recognition decision, as pure arithmetic (V1-02B0).

Deliberately free of OpenCV, of the database and of any model: it takes an already-extracted
query vector and the candidate staff templates and answers MATCH or UNKNOWN. That keeps the
part of recognition that can wrongly name a person fully testable in CI, on every platform,
without a model file.

Every template and every query vector is L2-normalised at extraction, so cosine similarity is
a dot product bounded to [-1, 1] and a larger score means more similar.

Aggregation. A teacher has three to five templates covering slightly different poses.
Averaging the *vectors* is not done: it destroys exactly the pose variation the photos were
collected for, and a mean vector's similarity to a query has no calibrated meaning. Taking the
single best template is the most false-accept-prone choice, because one unlucky template that
happens to sit near a stranger decides the whole identity. The aggregate used here is the mean
of the top two template scores (the single score when only one template exists), which needs
agreement from two independent enrollment photos before a person is named, and still lets a
teacher match from a pose that only some of their photos cover.

Two independent guards then have to pass, in this order:

* an absolute threshold, so a query that resembles nobody strongly enough stays UNKNOWN;
* a best-versus-second-best margin across *distinct staff*, so a query that resembles two
  different people almost equally stays UNKNOWN rather than picking the winner.

Both are evaluation values chosen conservatively, not production-calibrated numbers; a false
UNKNOWN is always preferable to a false identity.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

TEMPLATE_DTYPE = "float32"
# SFace emits 128 floats; the guard is a sanity bound, not a model-specific constant.
MAX_DIMENSIONS = 4096
MAX_CANDIDATE_TEMPLATES = 5
# An L2-normalised vector's norm after a float32 round-trip.
_NORM_TOLERANCE = 1e-3

Decision = Literal["MATCH", "UNKNOWN"]

# Why a query was not matched. Bounded and safe to return: it says nothing about who the
# candidates are, only why the decision refused to name one.
UnknownReason = Literal["no_candidates", "below_threshold", "ambiguous"]


class TemplateDecodeError(Exception):
    """A stored template is unusable. Bounded category; never the bytes."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class Vector:
    """An L2-normalised face vector. Never logged, never serialised, never returned."""

    values: tuple[float, ...]

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Vector(dim={len(self.values)}, REDACTED)"


@dataclass(frozen=True, slots=True, repr=False)
class StaffCandidate:
    """One enrolled teacher's usable templates, already filtered by the caller to ACTIVE
    profile, READY enrollment, ACTIVE templates and a matching model identity."""

    staff_id: UUID
    display_name: str
    vectors: tuple[Vector, ...]

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"StaffCandidate({self.staff_id}, templates={len(self.vectors)}, REDACTED)"


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    decision: Decision
    staff_id: UUID | None
    display_name: str | None
    score: float
    runner_up_score: float | None
    reason: UnknownReason | None


def normalize(values: list[float]) -> Vector:
    """L2-normalise a freshly extracted vector, rejecting anything unusable.

    Non-finite values are refused rather than propagated: a NaN silently poisons every
    comparison it takes part in, and ``nan >= threshold`` is False, so an unchecked NaN would
    turn into a silent, permanent UNKNOWN instead of a visible failure.
    """
    if not values or len(values) > MAX_DIMENSIONS:
        raise TemplateDecodeError("invalid_dimensions")
    if any(not math.isfinite(value) for value in values):
        raise TemplateDecodeError("non_finite_template")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise TemplateDecodeError("degenerate_template")
    return Vector(tuple(value / norm for value in values))


def encode(vector: Vector) -> bytes:
    """Pack a normalised vector into the stored little-endian float32 representation."""
    return struct.pack(f"<{len(vector.values)}f", *vector.values)


def decode(data: bytes, *, dimensions: int, dtype: str) -> Vector:
    """Unpack one stored template, verifying everything the database cannot enforce.

    The row's declared dimensions and dtype are checked against the bytes, and the vector is
    checked for finiteness and for still being normalised. A row that fails is a corrupt or
    foreign template, and the caller drops that candidate rather than comparing against it.
    """
    if dtype != TEMPLATE_DTYPE:
        raise TemplateDecodeError("unsupported_dtype")
    if dimensions < 1 or dimensions > MAX_DIMENSIONS:
        raise TemplateDecodeError("invalid_dimensions")
    if len(data) != dimensions * 4:
        raise TemplateDecodeError("template_size_mismatch")
    values = list(struct.unpack(f"<{dimensions}f", data))
    if any(not math.isfinite(value) for value in values):
        raise TemplateDecodeError("non_finite_template")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or abs(norm - 1.0) > _NORM_TOLERANCE:
        raise TemplateDecodeError("template_not_normalized")
    return Vector(tuple(values))


def similarity(left: Vector, right: Vector) -> float:
    """Cosine similarity of two normalised vectors, clamped to [-1, 1].

    Clamping is for float32 round-off only: a dot product of two unit vectors can land a few
    ulps outside the range, and a score of 1.0000000002 must never read as "more than certain".
    """
    if len(left.values) != len(right.values):
        raise TemplateDecodeError("dimension_mismatch")
    score = math.fsum(a * b for a, b in zip(left.values, right.values, strict=True))
    return max(-1.0, min(1.0, score))


def aggregate(query: Vector, candidate: StaffCandidate) -> float:
    """One teacher's score: the mean of their two best template similarities.

    Ordering is by score, so the result does not depend on the order the templates were read
    in; equal scores contribute equally and cannot change the mean.
    """
    scores = sorted((similarity(query, vector) for vector in candidate.vectors), reverse=True)
    if not scores:
        raise TemplateDecodeError("no_templates")
    best = scores[: min(2, len(scores))]
    return math.fsum(best) / len(best)


def recognize(
    query: Vector,
    candidates: tuple[StaffCandidate, ...],
    *,
    threshold: float,
    margin: float,
) -> RecognitionResult:
    """Decide who ``query`` is, or refuse to.

    ``candidates`` must already be ordered deterministically by the caller (by staff id), so
    that two candidates with identical aggregate scores always produce the same "best", and
    the ambiguity guard then keeps that arbitrary winner from being returned anyway.
    """
    if not candidates:
        return RecognitionResult("UNKNOWN", None, None, 0.0, None, "no_candidates")
    scored = sorted(
        ((aggregate(query, candidate), candidate) for candidate in candidates),
        # Ties resolve on staff id, never on list position, so the ordering is total.
        key=lambda pair: (-pair[0], str(pair[1].staff_id)),
    )
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    if best_score < threshold:
        return RecognitionResult("UNKNOWN", None, None, best_score, runner_up, "below_threshold")
    if runner_up is not None and (best_score - runner_up) < margin:
        return RecognitionResult("UNKNOWN", None, None, best_score, runner_up, "ambiguous")
    return RecognitionResult("MATCH", best.staff_id, best.display_name, best_score, runner_up, None)
