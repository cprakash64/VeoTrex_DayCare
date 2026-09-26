"""Guardian contacts, child <-> contact links and authorized child release (V1-04E).

Built on :class:`~veotrex_api.child_roster_service.ChildRosterService`, so every operation inherits
its scoping: the caller's tenant (RLS context plus explicit ``tenant_id`` predicates), one facility
at a time (READ_OPERATIONAL to read, ADMINISTER_FACILITY there to change anything or release a
child), and an unknown, other-tenant or unreadable identifier answered identically as
``not_found``.

A guardian contact is an operator's roster entry for an adult - a display name and an optional
identifier - never a biometric identity. Names and relationship labels are shown only to
authorised operators through these endpoints and never written to audit metadata or logs. Every
release names a child, a contact and a verification method explicitly and comes from an
authenticated operator; no camera, person track, recognition result or occupancy count can reach
these methods. Decisions are made by the pure :mod:`veotrex_api.guardian_release`.

**Release is one transaction.** Under the child's attendance lock (the same one check-in and
check-out take) it re-reads the attendance, the child, the contact and the pair's links, decides,
appends the CHECKED_OUT attendance event, appends the release event that references it and the
exact link, and writes one audit row. Any failure rolls all of it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.child_attendance import (
    AttendanceState,
    AttendanceTransition,
    AttendanceTransitionKind,
    ChildAttendanceError,
    ChildStatus,
    current_attendance,
    plan_check_out,
)
from veotrex_api.child_roster_service import (
    CHILD_CONFLICT_CATEGORIES,
    CHILD_VALIDATION_CATEGORIES,
    ChildRosterService,
    ClassroomAttendance,
)
from veotrex_api.child_roster_store import (
    attendance_record,
    latest_attendance_event,
    load_attendance_state,
    lock_child_attendance,
)
from veotrex_api.classroom_service import ClassroomError, utc
from veotrex_api.guardian_release import (
    GuardianReleaseError,
    GuardianStatus,
    LinkStatus,
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
from veotrex_api.guardian_store import (
    child_party,
    contact_party,
    link_terms,
    links_of_children,
    lock_guardian_link,
    pair_links,
)
from veotrex_api.models import (
    Area,
    ChildAttendanceEvent,
    ChildGuardianLink,
    ChildProfile,
    ChildReleaseEvent,
    Facility,
    GuardianContact,
)

MAX_GUARDIANS_PER_FACILITY = 3000
# ACTIVE links one child may have at once; history (INACTIVE rows) is kept without a cap.
MAX_ACTIVE_LINKS_PER_CHILD = 20
# How many links / releases one response returns. The rows are kept in full.
LINK_LIST_LIMIT = 100
RELEASE_HISTORY_LIMIT = 50

CHECKOUT_KIND_RELEASE = "AUTHORIZED_RELEASE"

GUARDIAN_VALIDATION_CATEGORIES = CHILD_VALIDATION_CATEGORIES | frozenset(
    {
        "invalid_guardian_status",
        "invalid_relationship_label",
        "invalid_link_note",
        "invalid_effective_period",
        "effective_period_inverted",
        "invalid_verification_method",
    }
)
RELEASE_REFUSAL_CATEGORIES = frozenset(
    decision.category for decision in PickupDecision if decision is not PickupDecision.AUTHORIZED
)
GUARDIAN_CONFLICT_CATEGORIES = (
    CHILD_CONFLICT_CATEGORIES
    | RELEASE_REFUSAL_CATEGORIES
    | frozenset(
        {
            "guardian_archived",
            "guardian_limit_reached",
            "association_exists",
            "association_limit_reached",
            "release_state_changed",
        }
    )
)


def _clean(error: ChildAttendanceError) -> ClassroomError:
    return ClassroomError(error.category)


# --------------------------------------------------------------------------- summaries
@dataclass(frozen=True, slots=True)
class GuardianSummary:
    guardian_contact_id: UUID
    facility_id: UUID
    display_name: str
    status: str
    external_reference: str | None
    active_link_count: int
    created_at: datetime
    updated_at: datetime
    can_administer: bool


@dataclass(frozen=True, slots=True)
class FacilityGuardians:
    facility_id: UUID
    facility_name: str
    facility_timezone: str
    can_administer: bool
    guardians: tuple[GuardianSummary, ...]


@dataclass(frozen=True, slots=True)
class LinkSummary:
    link_id: UUID
    child_profile_id: UUID
    guardian_contact_id: UUID
    guardian_display_name: str
    guardian_status: str
    relationship_label: str
    pickup_authorized: bool
    effective_from: datetime
    effective_until: datetime | None
    status: str
    note: str | None
    revision: int
    pickup_status: str  # AUTHORIZED or a PickupDecision refusal, evaluated now
    created_at: datetime
    updated_at: datetime
    deactivated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChildGuardians:
    child_profile_id: UUID
    child_display_name: str
    child_status: str
    facility_id: UUID
    facility_timezone: str
    can_administer: bool
    evaluated_at: datetime
    links: tuple[LinkSummary, ...]


@dataclass(frozen=True, slots=True)
class LinkInput:
    guardian_contact_id: UUID
    relationship_label: str
    pickup_authorized: bool
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LinkChange:
    relationship_label: str | None = None
    pickup_authorized: bool | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    set_effective_until: bool = False
    note: str | None = None
    set_note: bool = False


@dataclass(frozen=True, slots=True)
class ReleaseSummary:
    release_id: UUID
    classroom_id: UUID
    classroom_name: str
    child_profile_id: UUID
    guardian_contact_id: UUID
    guardian_display_name: str
    authorization_link_id: UUID
    authorization_link_revision: int
    verification_method: str
    released_at: datetime
    attendance_event_id: UUID
    recorded_by_caller: bool


@dataclass(frozen=True, slots=True)
class ChildReleaseHistory:
    child_profile_id: UUID
    releases: tuple[ReleaseSummary, ...]


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    guardian_contact_id: UUID
    display_name: str
    relationship_label: str
    link_id: UUID
    effective_until: datetime | None


@dataclass(frozen=True, slots=True)
class UnavailableContact:
    guardian_contact_id: UUID
    display_name: str
    relationship_label: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChildReleaseOptions:
    child_profile_id: UUID
    display_name: str
    candidates: tuple[ReleaseCandidate, ...]
    unavailable: tuple[UnavailableContact, ...]


@dataclass(frozen=True, slots=True)
class ClassroomReleaseOptions:
    classroom_id: UUID
    can_release: bool
    evaluated_at: datetime
    verification_methods: tuple[str, ...]
    children: tuple[ChildReleaseOptions, ...]


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    release: ReleaseSummary
    attendance: ClassroomAttendance


# ----------------------------------------------------------------------------- service
class GuardianService(ChildRosterService):
    # --------------------------------------------------------------------- helpers
    async def _guardian(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        guardian_id: UUID,
        *,
        administer: bool = False,
        for_update: bool = False,
    ) -> tuple[GuardianContact, Facility]:
        statement = select(GuardianContact).where(
            GuardianContact.id == guardian_id, GuardianContact.tenant_id == principal.tenant_id
        )
        if for_update:
            statement = statement.with_for_update()
        guardian = await session.scalar(statement)
        if guardian is None:
            raise ClassroomError("not_found")
        # An unreadable facility's contact is indistinguishable from an unknown one.
        facility = await self._facility(session, principal, guardian.facility_id)
        if administer and not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
            raise ClassroomError("access_denied")
        return guardian, facility

    async def _active_link_counts(
        self, session: AsyncSession, tenant_id: UUID, guardian_ids: list[UUID]
    ) -> dict[UUID, int]:
        if not guardian_ids:
            return {}
        rows = (
            await session.execute(
                select(ChildGuardianLink.guardian_contact_id, func.count())
                .where(
                    ChildGuardianLink.tenant_id == tenant_id,
                    ChildGuardianLink.guardian_contact_id.in_(guardian_ids),
                    ChildGuardianLink.status == str(LinkStatus.ACTIVE),
                )
                .group_by(ChildGuardianLink.guardian_contact_id)
            )
        ).all()
        return {row[0]: int(row[1]) for row in rows}

    def _guardian_summary(
        self, principal: AuthenticatedPrincipal, row: GuardianContact, active_links: int
    ) -> GuardianSummary:
        return GuardianSummary(
            guardian_contact_id=row.id,
            facility_id=row.facility_id,
            display_name=row.display_name,
            status=row.status,
            external_reference=row.external_reference,
            active_link_count=active_links,
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, row.facility_id),
        )

    async def _one_guardian_summary(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, row: GuardianContact
    ) -> GuardianSummary:
        counts = await self._active_link_counts(session, principal.tenant_id, [row.id])
        return self._guardian_summary(principal, row, counts.get(row.id, 0))

    @staticmethod
    def _link_summary(
        row: ChildGuardianLink, guardian: GuardianContact, child: ChildProfile, now: datetime
    ) -> LinkSummary:
        terms = link_terms(row)
        decision = decide_pickup(
            child=child_party(child),
            contact=contact_party(guardian),
            links=[terms],
            facility_id=child.facility_id,
            at=now,
        ).decision
        if terms.status is not LinkStatus.ACTIVE:
            # An old row explains itself, not whatever the pair's current link says.
            decision = link_decision(terms, now)
        return LinkSummary(
            link_id=row.id,
            child_profile_id=row.child_profile_id,
            guardian_contact_id=row.guardian_contact_id,
            guardian_display_name=guardian.display_name,
            guardian_status=guardian.status,
            relationship_label=row.relationship_label,
            pickup_authorized=row.pickup_authorized,
            effective_from=utc(row.effective_from),
            effective_until=None if row.effective_until is None else utc(row.effective_until),
            status=row.status,
            note=row.note,
            revision=row.revision,
            pickup_status=str(decision),
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            deactivated_at=None if row.deactivated_at is None else utc(row.deactivated_at),
        )

    @staticmethod
    def _link_audit(row: ChildGuardianLink) -> dict[str, Any]:
        """Bounded terms only: ids, the flag, the period, status and revision. Never the
        relationship label or the note."""
        return {
            "pickup_authorized": row.pickup_authorized,
            "effective_from": utc(row.effective_from).isoformat(),
            "effective_until": None
            if row.effective_until is None
            else utc(row.effective_until).isoformat(),
            "status": row.status,
            "revision": row.revision,
        }

    # ------------------------------------------------------------------ contacts
    async def list_guardians(
        self, principal: AuthenticatedPrincipal, facility_id: UUID
    ) -> FacilityGuardians:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._facility(session, principal, facility_id)
            rows = (
                await session.scalars(
                    select(GuardianContact)
                    .where(
                        GuardianContact.tenant_id == principal.tenant_id,
                        GuardianContact.facility_id == facility.id,
                    )
                    .order_by(GuardianContact.display_name, GuardianContact.id)
                )
            ).all()
            counts = await self._active_link_counts(
                session, principal.tenant_id, [row.id for row in rows]
            )
            return FacilityGuardians(
                facility_id=facility.id,
                facility_name=facility.name,
                facility_timezone=facility.timezone,
                can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
                guardians=tuple(
                    self._guardian_summary(principal, row, counts.get(row.id, 0)) for row in rows
                ),
            )

    async def get_guardian(
        self, principal: AuthenticatedPrincipal, guardian_id: UUID
    ) -> GuardianSummary:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            guardian, _ = await self._guardian(session, principal, guardian_id)
            return await self._one_guardian_summary(session, principal, guardian)

    async def create_guardian(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        display_name: str,
        external_reference: str | None,
        request_id: str,
    ) -> GuardianSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            name = clean_guardian_name(display_name)
            reference = clean_guardian_reference(external_reference)
        except GuardianReleaseError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._facility(session, principal, facility_id)
            if not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
                raise ClassroomError("access_denied")
            if facility.status != "ACTIVE":
                raise ClassroomError("facility_inactive")
            existing = await session.scalar(
                select(func.count())
                .select_from(GuardianContact)
                .where(
                    GuardianContact.tenant_id == principal.tenant_id,
                    GuardianContact.facility_id == facility.id,
                )
            )
            if int(existing or 0) >= MAX_GUARDIANS_PER_FACILITY:
                raise ClassroomError("guardian_limit_reached")
            guardian = GuardianContact(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                display_name=name,
                status=str(GuardianStatus.ACTIVE),
                external_reference=reference,
                created_by_actor_id=principal.actor_id,
            )
            session.add(guardian)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("external_reference_exists") from None
            # Ids and flags only: neither the name nor the reference is copied into the audit.
            self._audit(
                session,
                principal,
                "guardian_contact",
                guardian.id,
                "guardian.created",
                request_id,
                {
                    "facility_id": str(facility.id),
                    "status": guardian.status,
                    "external_reference_present": reference is not None,
                },
            )
            await session.flush()
            await session.refresh(guardian)
            return self._guardian_summary(principal, guardian, 0)

    async def update_guardian(
        self,
        principal: AuthenticatedPrincipal,
        guardian_id: UUID,
        request_id: str,
        *,
        display_name: str | None = None,
        external_reference: str | None = None,
        set_external_reference: bool = False,
    ) -> GuardianSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            name = None if display_name is None else clean_guardian_name(display_name)
            reference = clean_guardian_reference(external_reference)
        except GuardianReleaseError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            guardian, _ = await self._guardian(
                session, principal, guardian_id, administer=True, for_update=True
            )
            if guardian.status == GuardianStatus.ARCHIVED:
                raise ClassroomError("guardian_archived")
            changed: list[str] = []
            if name is not None and name != guardian.display_name:
                guardian.display_name = name
                changed.append("display_name")
            if set_external_reference and reference != guardian.external_reference:
                guardian.external_reference = reference
                changed.append("external_reference")
            if changed:
                try:
                    await session.flush()
                except IntegrityError:
                    raise ClassroomError("external_reference_exists") from None
                # Which fields changed - never their old or new values.
                self._audit(
                    session,
                    principal,
                    "guardian_contact",
                    guardian.id,
                    "guardian.updated",
                    request_id,
                    {"facility_id": str(guardian.facility_id), "changed_fields": changed},
                )
                await session.flush()
                await session.refresh(guardian)
            return await self._one_guardian_summary(session, principal, guardian)

    async def set_guardian_status(
        self,
        principal: AuthenticatedPrincipal,
        guardian_id: UUID,
        target: str,
        request_id: str,
    ) -> GuardianSummary:
        """ACTIVE <-> INACTIVE, or -> ARCHIVED (terminal). Idempotent. Links are kept as they
        are; an inactive or archived contact is simply never authorized."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            wanted = GuardianStatus(target)
        except ValueError:
            raise ClassroomError("invalid_guardian_status") from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            guardian, _ = await self._guardian(
                session, principal, guardian_id, administer=True, for_update=True
            )
            before = GuardianStatus(guardian.status)
            try:
                changes = guardian_status_transition(before, wanted)
            except GuardianReleaseError as exc:
                raise _clean(exc) from None
            if changes:
                guardian.status = str(wanted)
                action = {
                    GuardianStatus.ACTIVE: "guardian.activated",
                    GuardianStatus.INACTIVE: "guardian.deactivated",
                    GuardianStatus.ARCHIVED: "guardian.archived",
                }[wanted]
                self._audit(
                    session,
                    principal,
                    "guardian_contact",
                    guardian.id,
                    action,
                    request_id,
                    {
                        "facility_id": str(guardian.facility_id),
                        "from_status": str(before),
                        "to_status": str(wanted),
                    },
                )
                await session.flush()
                await session.refresh(guardian)
            return await self._one_guardian_summary(session, principal, guardian)

    # --------------------------------------------------------------------- links
    async def _child_guardians_view(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        child: ChildProfile,
        facility: Facility,
        now: datetime,
    ) -> ChildGuardians:
        pairs = await links_of_children(session, principal.tenant_id, [child.id])
        # ACTIVE links first, then history, newest first; bounded per response.
        ordered = sorted(pairs, key=lambda pair: (pair[0].status != str(LinkStatus.ACTIVE),))[
            :LINK_LIST_LIMIT
        ]
        return ChildGuardians(
            child_profile_id=child.id,
            child_display_name=child.display_name,
            child_status=child.status,
            facility_id=facility.id,
            facility_timezone=facility.timezone,
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
            evaluated_at=now,
            links=tuple(
                self._link_summary(link, guardian, child, now) for link, guardian in ordered
            ),
        )

    async def list_child_guardians(
        self, principal: AuthenticatedPrincipal, child_id: UUID, *, now: datetime | None = None
    ) -> ChildGuardians:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, facility = await self._child(session, principal, child_id)
            return await self._child_guardians_view(
                session, principal, child, facility, now or datetime.now(UTC)
            )

    async def _link_row(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        child_id: UUID,
        link_id: UUID,
        *,
        for_update: bool = False,
    ) -> ChildGuardianLink:
        statement = select(ChildGuardianLink).where(
            ChildGuardianLink.id == link_id,
            ChildGuardianLink.tenant_id == tenant_id,
            ChildGuardianLink.child_profile_id == child_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise ClassroomError("not_found")
        return row

    async def create_link(
        self,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        payload: LinkInput,
        request_id: str,
    ) -> ChildGuardians:
        """Associate one contact with one child. The relationship label is operator text with
        no meaning to VeoTrex; pickup authorization is the separate flag and period. One ACTIVE
        link per pair; a new period for the same pair is an edit, or a new link after
        deactivation."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        now = datetime.now(UTC)
        try:
            label = clean_relationship_label(payload.relationship_label)
            note = clean_link_note(payload.note)
            begins = payload.effective_from or now
            validate_period(begins, payload.effective_until)
        except GuardianReleaseError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, facility = await self._child(session, principal, child_id, administer=True)
            guardian, _ = await self._guardian(session, principal, payload.guardian_contact_id)
            if guardian.facility_id != child.facility_id:
                raise ClassroomError("facility_mismatch")
            if child.status == ChildStatus.ARCHIVED:
                raise ClassroomError("child_archived")
            if guardian.status == GuardianStatus.ARCHIVED:
                raise ClassroomError("guardian_archived")
            await lock_guardian_link(session, principal.tenant_id, child.id, guardian.id)
            existing = await pair_links(session, principal.tenant_id, child.id, guardian.id)
            if any(row.status == LinkStatus.ACTIVE for row in existing):
                raise ClassroomError("association_exists")
            active_for_child = await session.scalar(
                select(func.count())
                .select_from(ChildGuardianLink)
                .where(
                    ChildGuardianLink.tenant_id == principal.tenant_id,
                    ChildGuardianLink.child_profile_id == child.id,
                    ChildGuardianLink.status == str(LinkStatus.ACTIVE),
                )
            )
            if int(active_for_child or 0) >= MAX_ACTIVE_LINKS_PER_CHILD:
                raise ClassroomError("association_limit_reached")
            row = ChildGuardianLink(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=child.facility_id,
                child_profile_id=child.id,
                guardian_contact_id=guardian.id,
                relationship_label=label,
                pickup_authorized=payload.pickup_authorized,
                effective_from=begins,
                effective_until=payload.effective_until,
                status=str(LinkStatus.ACTIVE),
                note=note,
                revision=1,
                created_by_actor_id=principal.actor_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("association_exists") from None
            self._audit(
                session,
                principal,
                "child_guardian_link",
                row.id,
                "child_guardian_link.created",
                request_id,
                {
                    "facility_id": str(row.facility_id),
                    "child_profile_id": str(row.child_profile_id),
                    "guardian_contact_id": str(row.guardian_contact_id),
                    **self._link_audit(row),
                    "note_present": note is not None,
                },
            )
            await session.flush()
            return await self._child_guardians_view(session, principal, child, facility, now)

    async def update_link(
        self,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        link_id: UUID,
        change: LinkChange,
        request_id: str,
    ) -> ChildGuardians:
        """Change an ACTIVE link in place (revision + 1, bounded before/after audited). The child
        and the contact of a link never change."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            label = (
                None
                if change.relationship_label is None
                else clean_relationship_label(change.relationship_label)
            )
            note = clean_link_note(change.note)
        except GuardianReleaseError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, facility = await self._child(session, principal, child_id, administer=True)
            row = await self._link_row(session, principal.tenant_id, child.id, link_id)
            await lock_guardian_link(
                session, principal.tenant_id, child.id, row.guardian_contact_id
            )
            row = await self._link_row(
                session, principal.tenant_id, child.id, link_id, for_update=True
            )
            await session.refresh(row)
            if row.status != LinkStatus.ACTIVE:
                raise ClassroomError("association_inactive")
            if child.status == ChildStatus.ARCHIVED:
                raise ClassroomError("child_archived")
            guardian, _ = await self._guardian(session, principal, row.guardian_contact_id)
            if guardian.status == GuardianStatus.ARCHIVED:
                raise ClassroomError("guardian_archived")
            begins = change.effective_from or row.effective_from
            ends = change.effective_until if change.set_effective_until else row.effective_until
            try:
                validate_period(begins, ends)
            except GuardianReleaseError as exc:
                raise _clean(exc) from None
            before = self._link_audit(row)
            changed: list[str] = []
            if label is not None and label != row.relationship_label:
                row.relationship_label = label
                changed.append("relationship_label")
            if (
                change.pickup_authorized is not None
                and change.pickup_authorized != row.pickup_authorized
            ):
                row.pickup_authorized = change.pickup_authorized
                changed.append("pickup_authorized")
            if begins != row.effective_from:
                row.effective_from = begins
                changed.append("effective_from")
            if ends != row.effective_until:
                row.effective_until = ends
                changed.append("effective_until")
            if change.set_note and note != row.note:
                row.note = note
                changed.append("note")
            now = datetime.now(UTC)
            if changed:
                row.revision += 1
                await session.flush()
                action = "child_guardian_link.updated"
                if "pickup_authorized" in changed:
                    action = (
                        "child_guardian_link.pickup_enabled"
                        if row.pickup_authorized
                        else "child_guardian_link.pickup_disabled"
                    )
                # Field names for the free-text fields; values only for the bounded terms.
                self._audit(
                    session,
                    principal,
                    "child_guardian_link",
                    row.id,
                    action,
                    request_id,
                    {
                        "facility_id": str(row.facility_id),
                        "child_profile_id": str(row.child_profile_id),
                        "guardian_contact_id": str(row.guardian_contact_id),
                        "changed_fields": changed,
                        "before": before,
                        "after": self._link_audit(row),
                    },
                )
                await session.flush()
            return await self._child_guardians_view(session, principal, child, facility, now)

    async def deactivate_link(
        self,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        link_id: UUID,
        request_id: str,
    ) -> ChildGuardians:
        """End a link. Idempotent: a second call changes nothing and is not audited. Always
        permitted, whatever the child's or contact's status - it only ever removes authority."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, facility = await self._child(session, principal, child_id, administer=True)
            row = await self._link_row(session, principal.tenant_id, child.id, link_id)
            await lock_guardian_link(
                session, principal.tenant_id, child.id, row.guardian_contact_id
            )
            row = await self._link_row(
                session, principal.tenant_id, child.id, link_id, for_update=True
            )
            await session.refresh(row)
            now = datetime.now(UTC)
            if row.status == LinkStatus.ACTIVE:
                before = self._link_audit(row)
                row.status = str(LinkStatus.INACTIVE)
                row.revision += 1
                row.deactivated_at = now
                row.deactivated_by_actor_id = principal.actor_id
                await session.flush()
                self._audit(
                    session,
                    principal,
                    "child_guardian_link",
                    row.id,
                    "child_guardian_link.deactivated",
                    request_id,
                    {
                        "facility_id": str(row.facility_id),
                        "child_profile_id": str(row.child_profile_id),
                        "guardian_contact_id": str(row.guardian_contact_id),
                        "before": before,
                        "after": self._link_audit(row),
                    },
                )
                await session.flush()
            return await self._child_guardians_view(session, principal, child, facility, now)

    # ------------------------------------------------------------------- release
    async def release_options(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> ClassroomReleaseOptions:
        """For each ACTIVE child present in this classroom: the contacts who are authorized for
        pickup right now (the only selectable choices), and the other linked contacts with the
        reason they are not."""
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(session, principal, classroom_id)
            at = now or datetime.now(UTC)
            state = await load_attendance_state(session, principal.tenant_id, facility.id)
            events = state.events()
            present = [
                child
                for child in state.profiles
                if child.status == ChildStatus.ACTIVE
                and (current := current_attendance(events, child.id, at)).in_classroom(area.id)
                and current.state is AttendanceState.PRESENT
            ]
            pairs = await links_of_children(
                session, principal.tenant_id, [child.id for child in present]
            )
            entries: list[ChildReleaseOptions] = []
            for child in present:
                mine = [pair for pair in pairs if pair[0].child_profile_id == child.id]
                contacts: dict[UUID, GuardianContact] = {}
                for _, guardian in mine:
                    contacts.setdefault(guardian.id, guardian)
                candidates: list[ReleaseCandidate] = []
                unavailable: list[UnavailableContact] = []
                for guardian in sorted(
                    contacts.values(), key=lambda item: (item.display_name, str(item.id))
                ):
                    pair_rows = [link for link, owner in mine if owner.id == guardian.id]
                    decision = decide_pickup(
                        child=child_party(child),
                        contact=contact_party(guardian),
                        links=[link_terms(link) for link in pair_rows],
                        facility_id=facility.id,
                        at=at,
                    )
                    active = next(
                        (link for link in pair_rows if link.status == LinkStatus.ACTIVE), None
                    )
                    if active is None:
                        continue  # history only: not a contact for this child any more
                    if decision.authorized and decision.link is not None:
                        candidates.append(
                            ReleaseCandidate(
                                guardian_contact_id=guardian.id,
                                display_name=guardian.display_name,
                                relationship_label=active.relationship_label,
                                link_id=active.id,
                                effective_until=None
                                if active.effective_until is None
                                else utc(active.effective_until),
                            )
                        )
                    else:
                        unavailable.append(
                            UnavailableContact(
                                guardian_contact_id=guardian.id,
                                display_name=guardian.display_name,
                                relationship_label=active.relationship_label,
                                reason=str(decision.decision),
                            )
                        )
                entries.append(
                    ChildReleaseOptions(
                        child_profile_id=child.id,
                        display_name=child.display_name,
                        candidates=tuple(candidates),
                        unavailable=tuple(unavailable),
                    )
                )
            return ClassroomReleaseOptions(
                classroom_id=area.id,
                can_release=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
                evaluated_at=at,
                verification_methods=tuple(str(method) for method in VerificationMethod),
                children=tuple(entries),
            )

    async def release_child(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        child_id: UUID,
        guardian_contact_id: UUID,
        verification_method: str,
        request_id: str,
    ) -> ReleaseResult:
        """Release a child present in this classroom to an adult who is authorized for pickup
        now: check-out + release record + audit, atomically, under the child's attendance lock.
        """
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            method = parse_verification_method(verification_method)
        except GuardianReleaseError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            child = await session.scalar(
                select(ChildProfile).where(
                    ChildProfile.id == child_id,
                    ChildProfile.tenant_id == principal.tenant_id,
                    ChildProfile.facility_id == facility.id,
                )
            )
            if child is None:
                # Unknown, other-tenant and other-facility children are indistinguishable.
                raise ClassroomError("not_found")
            # A contact the caller cannot read is unknown; one at another readable facility is
            # refused by the decision below as FACILITY_MISMATCH.
            guardian, _ = await self._guardian(session, principal, guardian_contact_id)
            await lock_child_attendance(session, principal.tenant_id, child.id)
            # Everything the decision rests on is re-read after the lock, and held FOR SHARE so
            # a concurrent status or link change waits for this release (or it for them).
            # populate_existing refreshes the two objects already in the identity map in place.
            await session.execute(
                select(ChildProfile)
                .where(ChildProfile.id == child.id, ChildProfile.tenant_id == principal.tenant_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            await session.execute(
                select(GuardianContact)
                .where(
                    GuardianContact.id == guardian.id,
                    GuardianContact.tenant_id == principal.tenant_id,
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            links = await pair_links(
                session, principal.tenant_id, child.id, guardian.id, for_share=True
            )
            now = datetime.now(UTC)
            latest = await latest_attendance_event(session, principal.tenant_id, child.id)
            current = current_attendance(
                [] if latest is None else [attendance_record(latest)], child.id, now
            )
            if not current.in_classroom(area.id):
                raise ClassroomError(
                    "child_in_another_classroom"
                    if current.state is AttendanceState.PRESENT
                    else "child_not_checked_in"
                )
            if current.state is not AttendanceState.PRESENT:
                # A lapsed stay: the child may already have gone. An administrative check-out
                # closes it; a release record would claim a handover nobody saw.
                raise ClassroomError("attendance_expired")
            decision = decide_pickup(
                child=child_party(child),
                contact=contact_party(guardian),
                links=[link_terms(link) for link in links],
                facility_id=facility.id,
                at=now,
            )
            if not decision.authorized or decision.link is None:
                raise ClassroomError(decision.decision.category)
            try:
                transition = plan_check_out(current, classroom_id=area.id, now=now)
            except ChildAttendanceError as exc:
                raise _clean(exc) from None
            if transition.kind is not AttendanceTransitionKind.CHECKED_OUT:
                raise ClassroomError("child_not_checked_in")
            checkout = self._append(session, principal, child.id, transition)[-1]
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("attendance_state_changed") from None
            release = self._release_row(
                principal,
                checkout,
                guardian.id,
                decision.link.link_id,
                decision.link.revision,
                method,
            )
            session.add(release)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("release_state_changed") from None
            self._audit_release(session, principal, transition, release, request_id)
            await session.flush()
            summary = ReleaseSummary(
                release_id=release.id,
                classroom_id=area.id,
                classroom_name=area.name,
                child_profile_id=child.id,
                guardian_contact_id=guardian.id,
                guardian_display_name=guardian.display_name,
                authorization_link_id=release.authorization_link_id,
                authorization_link_revision=release.authorization_link_revision,
                verification_method=release.verification_method,
                released_at=utc(release.released_at),
                attendance_event_id=release.attendance_event_id,
                recorded_by_caller=True,
            )
            view = await self._attendance_view(session, principal, area, facility, now)
            return ReleaseResult(release=summary, attendance=view)

    @staticmethod
    def _release_row(
        principal: AuthenticatedPrincipal,
        checkout: ChildAttendanceEvent,
        guardian_id: UUID,
        link_id: UUID,
        link_revision: int,
        method: VerificationMethod,
    ) -> ChildReleaseEvent:
        return ChildReleaseEvent(
            id=uuid4(),
            tenant_id=principal.tenant_id,
            facility_id=checkout.facility_id,
            area_id=checkout.area_id,
            child_profile_id=checkout.child_profile_id,
            guardian_contact_id=guardian_id,
            authorization_link_id=link_id,
            authorization_link_revision=link_revision,
            verification_method=str(method),
            attendance_event_id=checkout.id,
            attendance_event_type=checkout.event_type,
            released_at=checkout.occurred_at,
            recorded_by_actor_id=principal.actor_id,
        )

    def _audit_release(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        transition: AttendanceTransition,
        release: ChildReleaseEvent,
        request_id: str,
    ) -> None:
        """Ids, times, the method and bounded states only: no name, label, note or image."""
        self._audit(
            session,
            principal,
            "child_release_event",
            release.id,
            "child.released",
            request_id,
            {
                "classroom_id": str(release.area_id),
                "facility_id": str(release.facility_id),
                "child_profile_id": str(release.child_profile_id),
                "guardian_contact_id": str(release.guardian_contact_id),
                "authorization_link_id": str(release.authorization_link_id),
                "authorization_link_revision": release.authorization_link_revision,
                "verification_method": release.verification_method,
                "attendance_event_id": str(release.attendance_event_id),
                "checkout_kind": CHECKOUT_KIND_RELEASE,
                "previous_state": str(transition.previous.state),
                "released_at": utc(release.released_at).isoformat(),
            },
        )

    async def release_history(
        self, principal: AuthenticatedPrincipal, child_id: UUID
    ) -> ChildReleaseHistory:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, _ = await self._child(session, principal, child_id)
            rows = (
                await session.execute(
                    select(ChildReleaseEvent, GuardianContact.display_name, Area.name)
                    .join(
                        GuardianContact,
                        (GuardianContact.id == ChildReleaseEvent.guardian_contact_id)
                        & (GuardianContact.tenant_id == ChildReleaseEvent.tenant_id),
                    )
                    .join(
                        Area,
                        (Area.id == ChildReleaseEvent.area_id)
                        & (Area.tenant_id == ChildReleaseEvent.tenant_id),
                    )
                    .where(
                        ChildReleaseEvent.tenant_id == principal.tenant_id,
                        ChildReleaseEvent.child_profile_id == child.id,
                    )
                    .order_by(ChildReleaseEvent.released_at.desc(), ChildReleaseEvent.id.desc())
                    .limit(RELEASE_HISTORY_LIMIT)
                )
            ).all()
            return ChildReleaseHistory(
                child_profile_id=child.id,
                releases=tuple(
                    ReleaseSummary(
                        release_id=row[0].id,
                        classroom_id=row[0].area_id,
                        classroom_name=row[2],
                        child_profile_id=row[0].child_profile_id,
                        guardian_contact_id=row[0].guardian_contact_id,
                        guardian_display_name=row[1],
                        authorization_link_id=row[0].authorization_link_id,
                        authorization_link_revision=row[0].authorization_link_revision,
                        verification_method=row[0].verification_method,
                        released_at=utc(row[0].released_at),
                        attendance_event_id=row[0].attendance_event_id,
                        recorded_by_caller=row[0].recorded_by_actor_id == principal.actor_id,
                    )
                    for row in rows
                ),
            )
