"""The recognition decision itself (V1-02B0).

No database, no OpenCV, no model file: this is the arithmetic that decides whether a person is
named, so it is tested exhaustively and on every platform CI runs on. Vectors are constructed
by hand at known angles, which makes every expected similarity a cosine that can be written
down rather than measured.
"""

from __future__ import annotations

import math
import struct
from uuid import UUID

import pytest

from veotrex_api.face_matching import (
    RecognitionResult,
    StaffCandidate,
    TemplateDecodeError,
    Vector,
    aggregate,
    decode,
    encode,
    normalize,
    recognize,
    similarity,
)

DIMENSIONS = 128
ALICE = UUID("00000000-0000-0000-0000-0000000000a1")
BOB = UUID("00000000-0000-0000-0000-0000000000b2")


def at(degrees: float) -> Vector:
    """A unit vector at a known angle in the first two dimensions; the rest are zero, so the
    cosine similarity of two of them is exactly the cosine of their angular difference."""
    radians = math.radians(degrees)
    return normalize([math.cos(radians), math.sin(radians)] + [0.0] * (DIMENSIONS - 2))


def staff(staff_id: UUID, name: str, *degrees: float) -> StaffCandidate:
    return StaffCandidate(staff_id, name, tuple(at(value) for value in degrees))


# ------------------------------------------------------------------------------ normalisation
def test_normalize_produces_a_unit_vector() -> None:
    vector = normalize([3.0, 4.0] + [0.0] * (DIMENSIONS - 2))
    assert math.isclose(math.sqrt(sum(value**2 for value in vector.values)), 1.0, abs_tol=1e-9)
    assert math.isclose(vector.values[0], 0.6, abs_tol=1e-9)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_normalize_refuses_non_finite_values(bad: float) -> None:
    with pytest.raises(TemplateDecodeError) as raised:
        normalize([bad] + [1.0] * (DIMENSIONS - 1))
    assert raised.value.category == "non_finite_template"


def test_normalize_refuses_a_zero_vector() -> None:
    with pytest.raises(TemplateDecodeError) as raised:
        normalize([0.0] * DIMENSIONS)
    assert raised.value.category == "degenerate_template"


def test_normalize_refuses_an_empty_or_oversized_vector() -> None:
    for values in ([], [1.0] * 8193):
        with pytest.raises(TemplateDecodeError) as raised:
            normalize(values)
        assert raised.value.category == "invalid_dimensions"


# ------------------------------------------------------------------------------------ decoding
def test_encode_decode_round_trips() -> None:
    original = at(37.0)
    restored = decode(encode(original), dimensions=DIMENSIONS, dtype="float32")
    assert math.isclose(similarity(original, restored), 1.0, abs_tol=1e-6)


def test_decode_refuses_a_foreign_dtype() -> None:
    with pytest.raises(TemplateDecodeError) as raised:
        decode(encode(at(0.0)), dimensions=DIMENSIONS, dtype="float64")
    assert raised.value.category == "unsupported_dtype"


def test_decode_refuses_a_row_whose_declared_dimensions_disagree_with_its_bytes() -> None:
    with pytest.raises(TemplateDecodeError) as raised:
        decode(encode(at(0.0)), dimensions=64, dtype="float32")
    assert raised.value.category == "template_size_mismatch"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_decode_refuses_non_finite_stored_values(bad: float) -> None:
    values = list(at(0.0).values)
    values[7] = bad
    with pytest.raises(TemplateDecodeError) as raised:
        decode(struct.pack(f"<{DIMENSIONS}f", *values), dimensions=DIMENSIONS, dtype="float32")
    assert raised.value.category == "non_finite_template"


def test_decode_refuses_a_stored_vector_that_is_not_normalised() -> None:
    values = [value * 3.0 for value in at(12.0).values]
    with pytest.raises(TemplateDecodeError) as raised:
        decode(struct.pack(f"<{DIMENSIONS}f", *values), dimensions=DIMENSIONS, dtype="float32")
    assert raised.value.category == "template_not_normalized"


# ---------------------------------------------------------------------------------- similarity
def test_similarity_is_the_cosine_of_the_angle_between_two_vectors() -> None:
    assert math.isclose(similarity(at(0.0), at(0.0)), 1.0, abs_tol=1e-9)
    assert math.isclose(similarity(at(0.0), at(60.0)), 0.5, abs_tol=1e-9)
    assert math.isclose(similarity(at(0.0), at(90.0)), 0.0, abs_tol=1e-9)
    assert math.isclose(similarity(at(0.0), at(180.0)), -1.0, abs_tol=1e-9)


def test_similarity_never_exceeds_one_even_with_float_round_off() -> None:
    assert -1.0 <= similarity(at(0.0), at(0.0)) <= 1.0


def test_similarity_refuses_vectors_of_different_lengths() -> None:
    with pytest.raises(TemplateDecodeError) as raised:
        similarity(at(0.0), Vector((1.0, 0.0)))
    assert raised.value.category == "dimension_mismatch"


# ---------------------------------------------------------------------------------- aggregation
def test_aggregate_is_the_mean_of_the_two_best_templates() -> None:
    candidate = staff(ALICE, "Alice", 0.0, 60.0, 90.0)
    # cos 0 = 1.0 and cos 60 = 0.5 are the two best; cos 90 = 0.0 is discarded.
    assert math.isclose(aggregate(at(0.0), candidate), 0.75, abs_tol=1e-9)


def test_aggregate_of_a_single_template_is_that_template() -> None:
    assert math.isclose(aggregate(at(0.0), staff(ALICE, "Alice", 60.0)), 0.5, abs_tol=1e-9)


def test_aggregate_does_not_depend_on_the_order_templates_were_read_in() -> None:
    forwards = staff(ALICE, "Alice", 0.0, 30.0, 60.0, 80.0)
    backwards = staff(ALICE, "Alice", 80.0, 60.0, 30.0, 0.0)
    assert aggregate(at(5.0), forwards) == aggregate(at(5.0), backwards)


def test_one_strong_template_cannot_carry_an_identity_on_its_own() -> None:
    """The false-accept mode the aggregate exists to stop: a stranger who happens to sit close
    to one enrollment photo (cosine 0.8) while the rest of that teacher's photos disagree.

    Taking the best template alone would score 0.8 and name the person. Requiring two photos
    to agree scores below the threshold, so the answer is UNKNOWN.
    """
    candidate = staff(ALICE, "Alice", 36.87, 85.0, 86.0)
    best = max(similarity(at(0.0), vector) for vector in candidate.vectors)
    assert best > 0.45
    assert aggregate(at(0.0), candidate) < 0.45


# ----------------------------------------------------------------------------------- decisions
def decide(query: float, *candidates: StaffCandidate) -> RecognitionResult:
    return recognize(at(query), candidates, threshold=0.45, margin=0.06)


def test_a_clear_match_names_the_person() -> None:
    result = decide(11.0, staff(ALICE, "Alice", 10.0, 12.0, 14.0), staff(BOB, "Bob", 98.0, 100.0))
    assert result.decision == "MATCH"
    assert result.staff_id == ALICE
    assert result.display_name == "Alice"
    assert result.score > 0.99
    assert result.reason is None


def test_a_query_resembling_nobody_is_unknown() -> None:
    result = decide(190.0, staff(ALICE, "Alice", 10.0, 12.0, 14.0), staff(BOB, "Bob", 98.0, 100.0))
    assert result.decision == "UNKNOWN"
    assert result.reason == "below_threshold"
    assert result.staff_id is None and result.display_name is None


def test_a_query_between_two_people_is_unknown_even_though_both_clear_the_threshold() -> None:
    """The dangerous case: a stranger who looks somewhat like two enrolled teachers. Both
    aggregates are comfortably above the threshold, and the answer must still be UNKNOWN."""
    alice = staff(ALICE, "Alice", 10.0, 12.0, 14.0)
    bob = staff(BOB, "Bob", 98.0, 100.0, 102.0)
    result = decide(56.0, alice, bob)
    assert result.decision == "UNKNOWN"
    assert result.reason == "ambiguous"
    assert result.score > 0.45
    assert result.staff_id is None


def test_the_margin_is_measured_between_people_not_between_templates() -> None:
    """One person's own templates are near-identical; that must never read as ambiguity."""
    result = decide(10.0, staff(ALICE, "Alice", 10.0, 10.5, 11.0))
    assert result.decision == "MATCH"


def test_no_candidates_is_unknown_rather_than_an_error() -> None:
    result = recognize(at(0.0), (), threshold=0.45, margin=0.06)
    assert result.decision == "UNKNOWN"
    assert result.reason == "no_candidates"
    assert result.score == 0.0


def test_a_single_candidate_has_no_runner_up_and_no_ambiguity_guard() -> None:
    result = decide(12.0, staff(ALICE, "Alice", 10.0, 12.0))
    assert result.decision == "MATCH"
    assert result.runner_up_score is None


def test_exactly_at_the_threshold_matches_and_just_below_it_does_not() -> None:
    at_threshold = recognize(at(0.0), (staff(ALICE, "Alice", 60.0),), threshold=0.5, margin=0.0)
    assert at_threshold.decision == "MATCH"
    below = recognize(at(0.0), (staff(ALICE, "Alice", 60.0),), threshold=0.500001, margin=0.0)
    assert below.decision == "UNKNOWN"
    assert below.reason == "below_threshold"


def test_the_winner_of_a_tie_is_stable_and_is_refused_anyway() -> None:
    """Two identical candidates produce identical aggregates. The ordering must be total so the
    result is reproducible, and the margin must then refuse to return either of them."""
    first = decide(0.0, staff(ALICE, "Alice", 0.0), staff(BOB, "Bob", 0.0))
    second = decide(0.0, staff(BOB, "Bob", 0.0), staff(ALICE, "Alice", 0.0))
    assert first == second
    assert first.decision == "UNKNOWN"
    assert first.reason == "ambiguous"


def test_a_zero_margin_still_applies_the_threshold() -> None:
    result = recognize(at(89.0), (staff(ALICE, "Alice", 0.0),), threshold=0.45, margin=0.0)
    assert result.decision == "UNKNOWN"


def test_a_result_never_carries_an_identity_it_refused_to_name() -> None:
    for query in (190.0, 56.0):
        result = decide(query, staff(ALICE, "Alice", 10.0, 12.0), staff(BOB, "Bob", 98.0, 100.0))
        assert result.decision == "UNKNOWN"
        assert result.staff_id is None
        assert result.display_name is None


def test_vectors_do_not_appear_in_their_own_repr() -> None:
    """A vector reaching a log line or a traceback must not carry the numbers with it."""
    assert "REDACTED" in repr(at(0.0))
    assert "0." not in repr(at(0.0)).replace("dim=128", "")
    candidate = staff(ALICE, "Alice", 10.0)
    assert "REDACTED" in repr(candidate)
