"""The pure guardian / pickup-authorization core (V1-04E): text rules, lifecycle, verification
methods and the release decision engine. No database, clock or network; synthetic ids only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from veotrex_api.child_attendance import ChildStatus
from veotrex_api.guardian_release import (
    LINK_NOTE_MAX,
    RELATIONSHIP_LABEL_MAX,
    ChildParty,
    ContactParty,
    GuardianReleaseError,
    GuardianStatus,
    LinkStatus,
    LinkTerms,
    PickupDecision,
    VerificationMethod,
    clean_guardian_name,
    clean_guardian_reference,
    clean_link_note,
    clean_relationship_label,
    decide_pickup,
    guardian_status_transition,
    link_decision,
    parse_verification_method,
    validate_period,
)

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
FACILITY = UUID("11111111-1111-4111-8111-111111111111")
OTHER_FACILITY = UUID("22222222-2222-4222-8222-222222222222")
CHILD = UUID("33333333-3333-4333-8333-333333333333")
CONTACT = UUID("44444444-4444-4444-8444-444444444444")


def child(**changes: object) -> ChildParty:
    return replace(ChildParty(CHILD, FACILITY, ChildStatus.ACTIVE), **changes)  # type: ignore[arg-type]


def contact(**changes: object) -> ContactParty:
    return replace(ContactParty(CONTACT, FACILITY, GuardianStatus.ACTIVE), **changes)  # type: ignore[arg-type]


def link(**changes: object) -> LinkTerms:
    base = LinkTerms(
        link_id=uuid4(),
        child_profile_id=CHILD,
        guardian_contact_id=CONTACT,
        facility_id=FACILITY,
        status=LinkStatus.ACTIVE,
        pickup_authorized=True,
        effective_from=NOW - timedelta(days=1),
        effective_until=None,
        revision=1,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def decide(*links: LinkTerms, at: datetime = NOW, **parties: object) -> PickupDecision:
    return decide_pickup(
        child=parties.get("child_party", child()),  # type: ignore[arg-type]
        contact=parties.get("contact_party", contact()),  # type: ignore[arg-type]
        links=links,
        facility_id=parties.get("facility_id", FACILITY),  # type: ignore[arg-type]
        at=at,
    ).decision


# ===================================================================== text and lifecycle
def test_names_labels_and_notes_are_normalised_and_bounded() -> None:
    assert clean_guardian_name("  Maya   Sharma ") == "Maya Sharma"
    assert clean_relationship_label(" Family   friend ") == "Family friend"
    assert clean_link_note("  per enrolment form ") == "per enrolment form"
    assert clean_link_note("   ") is None and clean_link_note(None) is None
    assert clean_guardian_reference(" SIS-7 ") == "SIS-7"
    assert clean_relationship_label("x" * RELATIONSHIP_LABEL_MAX) == "x" * RELATIONSHIP_LABEL_MAX


@pytest.mark.parametrize(
    ("function", "value", "category"),
    [
        (clean_guardian_name, "", "invalid_display_name"),
        (clean_guardian_name, "x" * 121, "invalid_display_name"),
        (clean_guardian_name, "<b>Maya</b>", "invalid_display_name"),
        (clean_guardian_name, "Maya" + chr(0x200B) + "Sharma", "invalid_display_name"),
        (clean_guardian_name, "Maya\tSharma", "invalid_display_name"),
        (clean_relationship_label, "", "invalid_relationship_label"),
        (
            clean_relationship_label,
            "x" * (RELATIONSHIP_LABEL_MAX + 1),
            "invalid_relationship_label",
        ),
        (clean_relationship_label, "Mother<script>", "invalid_relationship_label"),
        (clean_relationship_label, "Mo" + chr(0x202E) + "ther", "invalid_relationship_label"),
        (clean_link_note, "x" * (LINK_NOTE_MAX + 1), "invalid_link_note"),
        (clean_link_note, "note\x00", "invalid_link_note"),
        (clean_guardian_reference, "has space", "invalid_external_reference"),
    ],
)
def test_invalid_text_is_refused_by_category(function: object, value: str, category: str) -> None:
    with pytest.raises(GuardianReleaseError) as refused:
        function(value)  # type: ignore[operator]
    assert refused.value.category == category
    assert value not in str(refused.value) or value == ""


def test_periods_must_be_aware_and_forward() -> None:
    validate_period(NOW, None)
    validate_period(NOW, NOW + timedelta(hours=10))
    for until in (NOW, NOW - timedelta(seconds=1)):
        with pytest.raises(GuardianReleaseError, match="effective_period_inverted"):
            validate_period(NOW, until)
    with pytest.raises(GuardianReleaseError, match="invalid_effective_period"):
        validate_period(NOW.replace(tzinfo=None), None)
    with pytest.raises(GuardianReleaseError, match="invalid_effective_period"):
        validate_period(NOW, (NOW + timedelta(hours=1)).replace(tzinfo=None))


def test_contact_lifecycle_and_terminal_archive() -> None:
    assert guardian_status_transition(GuardianStatus.ACTIVE, GuardianStatus.INACTIVE) is True
    assert guardian_status_transition(GuardianStatus.INACTIVE, GuardianStatus.ACTIVE) is True
    assert guardian_status_transition(GuardianStatus.ACTIVE, GuardianStatus.ACTIVE) is False
    assert guardian_status_transition(GuardianStatus.INACTIVE, GuardianStatus.ARCHIVED) is True
    assert guardian_status_transition(GuardianStatus.ARCHIVED, GuardianStatus.ARCHIVED) is False
    for target in (GuardianStatus.ACTIVE, GuardianStatus.INACTIVE):
        with pytest.raises(GuardianReleaseError, match="guardian_archived"):
            guardian_status_transition(GuardianStatus.ARCHIVED, target)


def test_verification_methods_are_three_bounded_operator_statements() -> None:
    assert [str(method) for method in VerificationMethod] == [
        "KNOWN_TO_STAFF",
        "OPERATOR_CONFIRMED",
        "PHOTO_ID_CHECKED",
    ]
    assert parse_verification_method("OPERATOR_CONFIRMED") is VerificationMethod.OPERATOR_CONFIRMED
    for bad in ("", "operator_confirmed", "FACE_MATCH", "OTHER_MANUAL", "ID:123456", None, 1):
        with pytest.raises(GuardianReleaseError, match="invalid_verification_method"):
            parse_verification_method(bad)


def test_link_terms_refuse_malformed_values() -> None:
    with pytest.raises(GuardianReleaseError, match="invalid_pickup_authorized"):
        link(pickup_authorized=1)
    with pytest.raises(GuardianReleaseError, match="invalid_revision"):
        link(revision=0)
    with pytest.raises(GuardianReleaseError, match="effective_period_inverted"):
        link(effective_until=NOW - timedelta(days=2))
    with pytest.raises(GuardianReleaseError, match="invalid_link_status"):
        link(status="ACTIVE")


# ========================================================================== decision engine
def test_authorized() -> None:
    current = link()
    result = decide_pickup(
        child=child(), contact=contact(), links=[current], facility_id=FACILITY, at=NOW
    )
    assert result.decision is PickupDecision.AUTHORIZED and result.authorized
    assert result.link == current


def test_no_association() -> None:
    assert decide() is PickupDecision.NO_ASSOCIATION
    # A link of another child, or of another contact, is not an association of this pair.
    assert decide(link(child_profile_id=uuid4()), link(guardian_contact_id=uuid4())) is (
        PickupDecision.NO_ASSOCIATION
    )


def test_inactive_association() -> None:
    assert decide(link(status=LinkStatus.INACTIVE)) is PickupDecision.ASSOCIATION_INACTIVE


def test_pickup_disabled_whatever_the_relationship() -> None:
    # The label never reaches the engine: a "Father" link without the flag is not authorized.
    assert decide(link(pickup_authorized=False)) is PickupDecision.PICKUP_NOT_AUTHORIZED
    assert "relationship_label" not in LinkTerms.__dataclass_fields__


def test_not_started_and_expired_at_the_boundaries() -> None:
    temporary = link(
        effective_from=NOW + timedelta(hours=1), effective_until=NOW + timedelta(hours=11)
    )
    assert decide(temporary) is PickupDecision.AUTHORIZATION_NOT_STARTED
    assert decide(temporary, at=NOW + timedelta(hours=1)) is PickupDecision.AUTHORIZED
    assert (
        decide(temporary, at=NOW + timedelta(hours=11) - timedelta(microseconds=1))
        is PickupDecision.AUTHORIZED
    )
    assert decide(temporary, at=NOW + timedelta(hours=11)) is PickupDecision.AUTHORIZATION_EXPIRED
    assert (
        decide(link(effective_until=NOW - timedelta(minutes=1)))
        is PickupDecision.AUTHORIZATION_EXPIRED
    )


def test_inactive_or_archived_child() -> None:
    for status in (ChildStatus.INACTIVE, ChildStatus.ARCHIVED):
        assert decide(link(), child_party=child(status=status)) is PickupDecision.CHILD_INACTIVE


def test_inactive_or_archived_contact() -> None:
    for status in (GuardianStatus.INACTIVE, GuardianStatus.ARCHIVED):
        assert (
            decide(link(), contact_party=contact(status=status))
            is PickupDecision.AUTHORIZED_PERSON_INACTIVE
        )


def test_facility_mismatch() -> None:
    assert decide(link(), facility_id=OTHER_FACILITY) is PickupDecision.FACILITY_MISMATCH
    assert (
        decide(link(), contact_party=contact(facility_id=OTHER_FACILITY))
        is PickupDecision.FACILITY_MISMATCH
    )
    assert (
        decide(link(), child_party=child(facility_id=OTHER_FACILITY))
        is PickupDecision.FACILITY_MISMATCH
    )
    assert decide(link(facility_id=OTHER_FACILITY)) is PickupDecision.FACILITY_MISMATCH


def test_the_refusal_order_is_fixed() -> None:
    # Every failure at once: the facility check wins, then the child, then the contact.
    everything = decide(
        link(status=LinkStatus.INACTIVE, pickup_authorized=False),
        child_party=child(status=ChildStatus.INACTIVE),
        contact_party=contact(status=GuardianStatus.INACTIVE),
        facility_id=OTHER_FACILITY,
    )
    assert everything is PickupDecision.FACILITY_MISMATCH
    assert (
        decide(
            child_party=child(status=ChildStatus.INACTIVE),
            contact_party=contact(status=GuardianStatus.INACTIVE),
        )
        is PickupDecision.CHILD_INACTIVE
    )
    assert (
        decide(contact_party=contact(status=GuardianStatus.INACTIVE))
        is PickupDecision.AUTHORIZED_PERSON_INACTIVE
    )


def test_an_older_link_never_revives_after_the_current_one_is_deactivated() -> None:
    older = link(effective_from=NOW - timedelta(days=30), status=LinkStatus.INACTIVE)
    newer = link(status=LinkStatus.INACTIVE, revision=3)
    assert decide(older, newer) is PickupDecision.ASSOCIATION_INACTIVE
    # And an active link is used even when an inactive one would have authorized.
    assert (
        decide(link(status=LinkStatus.INACTIVE), link(pickup_authorized=False))
        is PickupDecision.PICKUP_NOT_AUTHORIZED
    )


def test_two_active_links_fail_closed() -> None:
    assert decide(link(), link()) is PickupDecision.ASSOCIATION_AMBIGUOUS


def test_the_decision_is_deterministic_whatever_the_link_order() -> None:
    links = [link(status=LinkStatus.INACTIVE), link(), link(child_profile_id=uuid4())]
    assert {decide(*order) for order in (links, links[::-1], links[1:] + links[:1])} == {
        PickupDecision.AUTHORIZED
    }


def test_single_link_decision_explains_history_rows() -> None:
    assert link_decision(link(status=LinkStatus.INACTIVE), NOW) is (
        PickupDecision.ASSOCIATION_INACTIVE
    )
    assert link_decision(link(), NOW) is PickupDecision.AUTHORIZED


def test_refusal_categories_are_bounded_api_categories() -> None:
    for decision in PickupDecision:
        assert decision.category == decision.value.lower()
        assert decision.category.replace("_", "").isalpha()


def test_naive_evaluation_time_is_refused() -> None:
    with pytest.raises(GuardianReleaseError, match="evaluation_time_must_be_utc_aware"):
        decide(link(), at=NOW.replace(tzinfo=None))


def test_the_engine_takes_no_name_image_track_or_recognition_input() -> None:
    import inspect

    parameters = set(inspect.signature(decide_pickup).parameters)
    assert parameters == {"child", "contact", "links", "facility_id", "at"}
    for party in (ChildParty, ContactParty, LinkTerms):
        for field in party.__dataclass_fields__:
            for forbidden in ("name", "label", "face", "image", "photo", "track", "embedding"):
                assert forbidden not in field, (party.__name__, field)
