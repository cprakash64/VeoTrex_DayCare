"""The identifier scrub used by the "no forbidden word in this output" assertions."""

from __future__ import annotations

from uuid import UUID

from tests_text import without_identifiers


def test_a_uuid_or_digest_that_spells_a_forbidden_word_is_not_a_finding() -> None:
    # The shape of the CI failure: a random UUID ending in hex that spells "face".
    identifier = str(UUID("a1b2c3d4-0000-4000-8000-f47f0fbcface"))
    digest = "sha256:" + "0" * 56 + "facefeed"
    scrubbed = without_identifiers(f"{{'id': '{identifier}', 'revision': '{digest}'}}")
    assert "face" not in scrubbed and scrubbed.count("<id>") == 2


def test_a_forbidden_word_outside_an_identifier_is_still_found() -> None:
    for text in (
        "{'note': 'face match'}",
        "{'field': 'surface'}",
        "facefeed-not-a-uuid",
        "sha256:face",  # too short to be a digest
        "a1b2c3d4-0000-4000-8000-f47f0fbface",  # one hex digit short of a UUID
    ):
        assert "face" in without_identifiers(text), text
