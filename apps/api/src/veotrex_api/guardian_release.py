"""Guardian / contact association and authorized child release: the pure core (V1-04E).

Like :mod:`veotrex_api.child_attendance`, nothing here touches a database, a clock, a camera or a
network; every function takes ``at`` explicitly.

**A guardian contact is an operator's record of an adult, not an identity proof.** It holds an
opaque id, a display name for the operator's own screens, a status and an optional identifier for
a future external connector. There is no photo, face, embedding, voice, identity-document image or
number, date of birth, address or camera field anywhere, and nothing in this module accepts an
image, a track, an occupancy count or a recognition result. A camera never authorizes a release.

**Relationship is not authorization.** A link between one child and one contact carries an
operator-typed ``relationship_label`` ("Mother", "Family friend") that has no legal meaning and
is never interpreted, and a separate ``pickup_authorized`` flag with an effective period. A
"Father" with ``pickup_authorized = False`` is representable and is never authorized.

**Release is decided from ids, never names.** :func:`decide_pickup` takes the child, the contact
and every link between the two, all selected by id, and answers AUTHORIZED or one bounded
refusal. Only ACTIVE links are ever considered: deactivating the current link never revives an
older one, and two ACTIVE links for one pair (which the database forbids) fail closed.

**Verification is the operator's statement.** :class:`VerificationMethod` records how the
operator at the door says they confirmed the adult; VeoTrex does not verify anyone.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from veotrex_api.child_attendance import (
    ChildAttendanceError,
    ChildStatus,
    clean_display_name,
    clean_external_reference,
)

# The relationship label is operator text such as "Grandparent"; short, and no markup, control or
# invisible characters. It is shown only to operators and never written to audit metadata.
RELATIONSHIP_LABEL_MAX = 64
# The link note is optional operator reference text ("per enrolment form 2026-09"). Bounded, and
# never audited or shown outside the child's own authenticated page.
LINK_NOTE_MAX = 200
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_NO_MARKUP = re.compile(r"^[^<>]*$")


class GuardianReleaseError(ChildAttendanceError):
    """A guardian, link or release operation was refused. The category names the rule, never a
    person."""


def _aware(value: datetime, category: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GuardianReleaseError(category)


def _clean_text(value: object, *, maximum: int, category: str) -> str:
    if not isinstance(value, str):
        raise GuardianReleaseError(category)
    normalised = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in normalised.strip()):
        raise GuardianReleaseError(category)
    text = " ".join(normalised.split())
    if not text or len(text) > maximum or not _NO_MARKUP.match(text):
        raise GuardianReleaseError(category)
    return text


def clean_guardian_name(value: str) -> str:
    """The same rules as a child's display name: NFC, whitespace-collapsed, <= 120, no control,
    invisible or markup characters."""
    try:
        return clean_display_name(value)
    except ChildAttendanceError:
        raise GuardianReleaseError("invalid_display_name") from None


def clean_guardian_reference(value: str | None) -> str | None:
    try:
        return clean_external_reference(value)
    except ChildAttendanceError:
        raise GuardianReleaseError("invalid_external_reference") from None


def clean_relationship_label(value: str) -> str:
    return _clean_text(value, maximum=RELATIONSHIP_LABEL_MAX, category="invalid_relationship_label")


def clean_link_note(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _clean_text(value, maximum=LINK_NOTE_MAX, category="invalid_link_note")


def validate_period(effective_from: datetime, effective_until: datetime | None) -> None:
    """Half-open ``[from, until)``; ``until`` must be strictly later. Both timezone-aware."""
    _aware(effective_from, "invalid_effective_period")
    if effective_until is None:
        return
    _aware(effective_until, "invalid_effective_period")
    if effective_until <= effective_from:
        raise GuardianReleaseError("effective_period_inverted")


# ------------------------------------------------------------------------------- lifecycle
class GuardianStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"  # temporarily not a contact; can be reactivated
    ARCHIVED = "ARCHIVED"  # no longer a contact; terminal, kept for release history


def guardian_status_transition(current: GuardianStatus, target: GuardianStatus) -> bool:
    """ACTIVE <-> INACTIVE freely; either may be ARCHIVED; ARCHIVED is terminal. False means
    "already there" (idempotent)."""
    if current is target:
        return False
    if current is GuardianStatus.ARCHIVED:
        raise GuardianReleaseError("guardian_archived")
    return True


class LinkStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"  # terminal for the row; a new association is a new row


# ------------------------------------------------------------------------ verification
class VerificationMethod(StrEnum):
    """How the operator at pickup says they confirmed the adult in front of them.

    These are operator statements, recorded as such. VeoTrex scans, stores and authenticates
    nothing: ``PHOTO_ID_CHECKED`` means the operator reports having looked at an identity
    document - no image, number or document detail is captured. There is deliberately no
    free-text "other" method: ``OPERATOR_CONFIRMED`` already covers any other confirmation the
    operator stands behind, without inviting identity details into a note.
    """

    KNOWN_TO_STAFF = "KNOWN_TO_STAFF"
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    PHOTO_ID_CHECKED = "PHOTO_ID_CHECKED"


def parse_verification_method(value: object) -> VerificationMethod:
    if not isinstance(value, str):
        raise GuardianReleaseError("invalid_verification_method")
    try:
        return VerificationMethod(value)
    except ValueError:
        raise GuardianReleaseError("invalid_verification_method") from None


# ----------------------------------------------------------------------------- decision
class PickupDecision(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    FACILITY_MISMATCH = "FACILITY_MISMATCH"
    CHILD_INACTIVE = "CHILD_INACTIVE"
    AUTHORIZED_PERSON_INACTIVE = "AUTHORIZED_PERSON_INACTIVE"
    NO_ASSOCIATION = "NO_ASSOCIATION"
    ASSOCIATION_INACTIVE = "ASSOCIATION_INACTIVE"
    ASSOCIATION_AMBIGUOUS = "ASSOCIATION_AMBIGUOUS"
    PICKUP_NOT_AUTHORIZED = "PICKUP_NOT_AUTHORIZED"
    AUTHORIZATION_NOT_STARTED = "AUTHORIZATION_NOT_STARTED"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"

    @property
    def category(self) -> str:
        """The bounded API error category for a refusal."""
        return self.value.lower()


@dataclass(frozen=True, slots=True)
class ChildParty:
    """A child as the decision needs it: an opaque id, a facility and a status. No name."""

    child_profile_id: UUID
    facility_id: UUID
    status: ChildStatus


@dataclass(frozen=True, slots=True)
class ContactParty:
    """A guardian contact as the decision needs it: an opaque id, a facility and a status."""

    guardian_contact_id: UUID
    facility_id: UUID
    status: GuardianStatus


@dataclass(frozen=True, slots=True)
class LinkTerms:
    """One stored child <-> contact link: ids, the pickup flag, the period and the status. The
    relationship label is deliberately absent: it never influences a decision."""

    link_id: UUID
    child_profile_id: UUID
    guardian_contact_id: UUID
    facility_id: UUID
    status: LinkStatus
    pickup_authorized: bool
    effective_from: datetime
    effective_until: datetime | None
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, LinkStatus):
            raise GuardianReleaseError("invalid_link_status")
        if not isinstance(self.pickup_authorized, bool):
            raise GuardianReleaseError("invalid_pickup_authorized")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise GuardianReleaseError("invalid_revision")
        if self.revision < 1:
            raise GuardianReleaseError("invalid_revision")
        validate_period(self.effective_from, self.effective_until)


@dataclass(frozen=True, slots=True)
class PickupAuthorization:
    decision: PickupDecision
    link: LinkTerms | None  # the ACTIVE link the decision rests on, when there is exactly one

    @property
    def authorized(self) -> bool:
        return self.decision is PickupDecision.AUTHORIZED


def link_decision(link: LinkTerms, at: datetime) -> PickupDecision:
    """What one link alone says at ``at``: status, then the flag, then the period."""
    _aware(at, "evaluation_time_must_be_utc_aware")
    if link.status is not LinkStatus.ACTIVE:
        return PickupDecision.ASSOCIATION_INACTIVE
    if not link.pickup_authorized:
        return PickupDecision.PICKUP_NOT_AUTHORIZED
    if at < link.effective_from:
        return PickupDecision.AUTHORIZATION_NOT_STARTED
    if link.effective_until is not None and at >= link.effective_until:
        return PickupDecision.AUTHORIZATION_EXPIRED
    return PickupDecision.AUTHORIZED


def decide_pickup(
    *,
    child: ChildParty,
    contact: ContactParty,
    links: Iterable[LinkTerms],
    facility_id: UUID,
    at: datetime,
) -> PickupAuthorization:
    """child + contact + their links + the releasing classroom's facility + time -> decision.

    Checked in a fixed order, first failure wins:

    1. FACILITY_MISMATCH - the child, the contact or a link of the pair is not at ``facility_id``;
    2. CHILD_INACTIVE - the child profile is not ACTIVE;
    3. AUTHORIZED_PERSON_INACTIVE - the contact is not ACTIVE;
    4. NO_ASSOCIATION - no link between exactly this child and this contact;
    5. ASSOCIATION_INACTIVE - links exist, none is ACTIVE (older links never revive);
    6. ASSOCIATION_AMBIGUOUS - more than one ACTIVE link (the database forbids it; fail closed);
    7. PICKUP_NOT_AUTHORIZED - the ACTIVE link does not authorize pickup, whatever its label;
    8. AUTHORIZATION_NOT_STARTED / AUTHORIZATION_EXPIRED - outside ``[from, until)``.

    Links of any other child or contact are ignored. There is no name, label, image, track or
    recognition parameter: identity is the ids the operator selected.
    """
    _aware(at, "evaluation_time_must_be_utc_aware")
    pair = [
        link
        for link in links
        if link.child_profile_id == child.child_profile_id
        and link.guardian_contact_id == contact.guardian_contact_id
    ]
    if (
        child.facility_id != facility_id
        or contact.facility_id != facility_id
        or any(link.facility_id != facility_id for link in pair)
    ):
        return PickupAuthorization(PickupDecision.FACILITY_MISMATCH, None)
    if child.status is not ChildStatus.ACTIVE:
        return PickupAuthorization(PickupDecision.CHILD_INACTIVE, None)
    if contact.status is not GuardianStatus.ACTIVE:
        return PickupAuthorization(PickupDecision.AUTHORIZED_PERSON_INACTIVE, None)
    if not pair:
        return PickupAuthorization(PickupDecision.NO_ASSOCIATION, None)
    active = [link for link in pair if link.status is LinkStatus.ACTIVE]
    if not active:
        return PickupAuthorization(PickupDecision.ASSOCIATION_INACTIVE, None)
    if len(active) > 1:
        return PickupAuthorization(PickupDecision.ASSOCIATION_AMBIGUOUS, None)
    link = active[0]
    return PickupAuthorization(link_decision(link, at), link)
