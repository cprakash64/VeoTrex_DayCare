"""Facility child roster and child attendance check-in/out (V1-04D).

Built on :class:`~veotrex_api.classroom_service.ClassroomService`, so every operation inherits its
scoping: the caller's tenant (RLS context plus explicit ``tenant_id`` predicates), one facility
at a time (READ_OPERATIONAL to read, ADMINISTER_FACILITY there to change anything), and an
unknown, other-tenant, other-facility or unreadable identifier answered identically as
``not_found``.

A child profile is ordinary roster data an operator enters - never a biometric identity. The
display name is shown only to authorised operators through these endpoints; it is never written
to audit metadata or logs, never handed to the ratio engine, and never reachable from an edge
node. Every attendance write names a child profile explicitly and comes from an authenticated
operator: no camera, person track, recognition result or occupancy count can reach these
methods. Decisions are made by the pure :mod:`veotrex_api.child_attendance`.
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
    ATTENDANCE_LEASE_DEFAULT_SECONDS,
    ATTENDANCE_LEASE_MAX_SECONDS,
    ATTENDANCE_LEASE_MIN_SECONDS,
    AttendanceState,
    AttendanceTransition,
    AttendanceTransitionKind,
    ChildAttendanceError,
    ChildCountResolution,
    ChildStatus,
    clean_display_name,
    clean_external_reference,
    current_attendance,
    plan_check_in,
    plan_check_out,
    plan_refresh,
    resolve_child_count,
    status_transition,
    validate_attendance_lease,
)
from veotrex_api.child_roster_store import (
    attendance_record,
    latest_attendance_event,
    load_attendance_state,
    lock_child_attendance,
)
from veotrex_api.classroom_service import (
    CLASSROOM_KIND,
    ClassroomError,
    ClassroomService,
    utc,
)
from veotrex_api.models import Area, ChildAttendanceEvent, ChildProfile, Facility

MAX_CHILDREN_PER_FACILITY = 1000
# How many of a classroom's past attendance events one response returns. The stream is kept in
# full in the table; this only bounds a response.
ATTENDANCE_HISTORY_LIMIT = 20

CHILD_VALIDATION_CATEGORIES = frozenset(
    {
        "invalid_display_name",
        "invalid_external_reference",
        "invalid_lease_seconds",
        "invalid_child_status",
    }
)
CHILD_CONFLICT_CATEGORIES = frozenset(
    {
        "facility_inactive",
        "classroom_inactive",
        "child_not_active",
        "child_archived",
        "child_already_checked_in",
        "child_in_another_classroom",
        "child_not_checked_in",
        "attendance_expired",
        "external_reference_exists",
        "child_limit_reached",
        # Two writers raced past the lock (it cannot normally happen): the database kept one.
        "attendance_state_changed",
    }
)


# --------------------------------------------------------------------------- summaries
@dataclass(frozen=True, slots=True)
class ChildSummary:
    child_id: UUID
    facility_id: UUID
    display_name: str
    status: str
    external_reference: str | None
    created_at: datetime
    updated_at: datetime
    can_administer: bool


@dataclass(frozen=True, slots=True)
class FacilityChildren:
    facility_id: UUID
    facility_name: str
    can_administer: bool
    children: tuple[ChildSummary, ...]


@dataclass(frozen=True, slots=True)
class AttendanceEntry:
    child_profile_id: UUID
    display_name: str
    status: str
    counted: bool
    state: str  # PRESENT / STALE / NOT_CHECKED_IN
    location: str  # HERE / OTHER_CLASSROOM / NONE
    other_classroom_id: UUID | None
    other_classroom_name: str | None
    checked_in_at: datetime | None
    last_event_at: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class AttendanceEventSummary:
    event_id: UUID
    child_profile_id: UUID
    display_name: str
    event_type: str
    occurred_at: datetime
    valid_until: datetime | None
    recorded_by_caller: bool


@dataclass(frozen=True, slots=True)
class ClassroomAttendance:
    classroom_id: UUID
    facility_id: UUID
    classroom_active: bool
    presence_source_mode: str
    can_administer: bool
    evaluated_at: datetime
    summary: ChildCountResolution
    children: tuple[AttendanceEntry, ...]
    recent_events: tuple[AttendanceEventSummary, ...]
    lease_min_seconds: int = ATTENDANCE_LEASE_MIN_SECONDS
    lease_max_seconds: int = ATTENDANCE_LEASE_MAX_SECONDS
    lease_default_seconds: int = ATTENDANCE_LEASE_DEFAULT_SECONDS


def _clean(category_error: ChildAttendanceError) -> ClassroomError:
    return ClassroomError(category_error.category)


# ----------------------------------------------------------------------------- service
class ChildRosterService(ClassroomService):
    # --------------------------------------------------------------------- helpers
    async def _child(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        *,
        administer: bool = False,
        for_update: bool = False,
    ) -> tuple[ChildProfile, Facility]:
        statement = select(ChildProfile).where(
            ChildProfile.id == child_id, ChildProfile.tenant_id == principal.tenant_id
        )
        if for_update:
            statement = statement.with_for_update()
        child = await session.scalar(statement)
        if child is None:
            raise ClassroomError("not_found")
        # An unreadable facility's child is indistinguishable from an unknown one.
        facility = await self._facility(session, principal, child.facility_id)
        if administer and not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
            raise ClassroomError("access_denied")
        return child, facility

    def _summary_of(self, principal: AuthenticatedPrincipal, child: ChildProfile) -> ChildSummary:
        return ChildSummary(
            child_id=child.id,
            facility_id=child.facility_id,
            display_name=child.display_name,
            status=child.status,
            external_reference=child.external_reference,
            created_at=utc(child.created_at),
            updated_at=utc(child.updated_at),
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, child.facility_id),
        )

    # ---------------------------------------------------------------------- roster
    async def list_children(
        self, principal: AuthenticatedPrincipal, facility_id: UUID
    ) -> FacilityChildren:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._facility(session, principal, facility_id)
            rows = (
                await session.scalars(
                    select(ChildProfile)
                    .where(
                        ChildProfile.tenant_id == principal.tenant_id,
                        ChildProfile.facility_id == facility.id,
                    )
                    .order_by(ChildProfile.display_name, ChildProfile.id)
                )
            ).all()
            return FacilityChildren(
                facility_id=facility.id,
                facility_name=facility.name,
                can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
                children=tuple(self._summary_of(principal, row) for row in rows),
            )

    async def get_child(self, principal: AuthenticatedPrincipal, child_id: UUID) -> ChildSummary:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, _ = await self._child(session, principal, child_id)
            return self._summary_of(principal, child)

    async def create_child(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        display_name: str,
        external_reference: str | None,
        request_id: str,
    ) -> ChildSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            name = clean_display_name(display_name)
            reference = clean_external_reference(external_reference)
        except ChildAttendanceError as exc:
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
                .select_from(ChildProfile)
                .where(
                    ChildProfile.tenant_id == principal.tenant_id,
                    ChildProfile.facility_id == facility.id,
                )
            )
            if int(existing or 0) >= MAX_CHILDREN_PER_FACILITY:
                raise ClassroomError("child_limit_reached")
            child = ChildProfile(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                display_name=name,
                status=str(ChildStatus.ACTIVE),
                external_reference=reference,
                created_by_actor_id=principal.actor_id,
            )
            session.add(child)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("external_reference_exists") from None
            # Ids and flags only: neither the name nor the reference is copied into the audit.
            self._audit(
                session,
                principal,
                "child_profile",
                child.id,
                "child.created",
                request_id,
                {
                    "facility_id": str(facility.id),
                    "status": child.status,
                    "external_reference_present": reference is not None,
                },
            )
            await session.flush()
            await session.refresh(child)
            return self._summary_of(principal, child)

    async def update_child(
        self,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        request_id: str,
        *,
        display_name: str | None = None,
        external_reference: str | None = None,
        set_external_reference: bool = False,
    ) -> ChildSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            name = None if display_name is None else clean_display_name(display_name)
            reference = clean_external_reference(external_reference)
        except ChildAttendanceError as exc:
            raise _clean(exc) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, _ = await self._child(
                session, principal, child_id, administer=True, for_update=True
            )
            if child.status == ChildStatus.ARCHIVED:
                raise ClassroomError("child_archived")
            changed: list[str] = []
            if name is not None and name != child.display_name:
                child.display_name = name
                changed.append("display_name")
            if set_external_reference and reference != child.external_reference:
                child.external_reference = reference
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
                    "child_profile",
                    child.id,
                    "child.updated",
                    request_id,
                    {"facility_id": str(child.facility_id), "changed_fields": changed},
                )
                await session.flush()
                await session.refresh(child)
            return self._summary_of(principal, child)

    async def set_child_status(
        self,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        target: str,
        request_id: str,
    ) -> ChildSummary:
        """ACTIVE <-> INACTIVE, or -> ARCHIVED (terminal). Idempotent. A child who is checked in
        stays on the record until checked out or the lease lapses, but stops counting at once."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            wanted = ChildStatus(target)
        except ValueError:
            raise ClassroomError("invalid_child_status") from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            child, _ = await self._child(
                session, principal, child_id, administer=True, for_update=True
            )
            before = ChildStatus(child.status)
            try:
                changes = status_transition(before, wanted)
            except ChildAttendanceError as exc:
                raise _clean(exc) from None
            if changes:
                child.status = str(wanted)
                action = {
                    ChildStatus.ACTIVE: "child.activated",
                    ChildStatus.INACTIVE: "child.deactivated",
                    ChildStatus.ARCHIVED: "child.archived",
                }[wanted]
                self._audit(
                    session,
                    principal,
                    "child_profile",
                    child.id,
                    action,
                    request_id,
                    {
                        "facility_id": str(child.facility_id),
                        "from_status": str(before),
                        "to_status": str(wanted),
                    },
                )
                await session.flush()
                await session.refresh(child)
            return self._summary_of(principal, child)

    # ------------------------------------------------------------------ attendance
    async def _attendance_view(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        area: Area,
        facility: Facility,
        now: datetime,
    ) -> ClassroomAttendance:
        state = await load_attendance_state(session, principal.tenant_id, facility.id)
        events = state.events()
        summary = resolve_child_count(
            classroom_id=area.id,
            facility_id=facility.id,
            now=now,
            members=state.members(),
            events=events,
        )
        rooms: dict[UUID, str] = {
            row.id: row.name
            for row in (
                await session.execute(
                    select(Area.id, Area.name).where(
                        Area.tenant_id == principal.tenant_id,
                        Area.facility_id == facility.id,
                        Area.kind == CLASSROOM_KIND,
                    )
                )
            ).all()
        }
        entries: list[AttendanceEntry] = []
        for child in state.profiles:
            current = current_attendance(events, child.id, now)
            here = current.in_classroom(area.id)
            # Every ACTIVE child of the facility can be checked in here; anyone else is listed
            # only while they are still on this room's record.
            if child.status != ChildStatus.ACTIVE and not here:
                continue
            if here:
                location = "HERE"
            elif current.open:
                location = "OTHER_CLASSROOM"
            else:
                location = "NONE"
            elsewhere = location == "OTHER_CLASSROOM"
            entries.append(
                AttendanceEntry(
                    child_profile_id=child.id,
                    display_name=child.display_name,
                    status=child.status,
                    counted=here
                    and current.state is AttendanceState.PRESENT
                    and child.status == ChildStatus.ACTIVE,
                    state=str(current.state),
                    location=location,
                    other_classroom_id=current.classroom_id if elsewhere else None,
                    other_classroom_name=rooms.get(current.classroom_id)
                    if elsewhere and current.classroom_id is not None
                    else None,
                    checked_in_at=None
                    if current.checked_in_at is None
                    else utc(current.checked_in_at),
                    last_event_at=None
                    if current.last_event_at is None
                    else utc(current.last_event_at),
                    valid_until=None if current.valid_until is None else utc(current.valid_until),
                )
            )
        names = {child.id: child.display_name for child in state.profiles}
        history = (
            await session.scalars(
                select(ChildAttendanceEvent)
                .where(
                    ChildAttendanceEvent.tenant_id == principal.tenant_id,
                    ChildAttendanceEvent.area_id == area.id,
                )
                .order_by(
                    ChildAttendanceEvent.occurred_at.desc(),
                    ChildAttendanceEvent.sequence.desc(),
                    ChildAttendanceEvent.id.desc(),
                )
                .limit(ATTENDANCE_HISTORY_LIMIT)
            )
        ).all()
        return ClassroomAttendance(
            classroom_id=area.id,
            facility_id=facility.id,
            classroom_active=area.status == "ACTIVE",
            presence_source_mode=area.presence_source_mode,
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
            evaluated_at=now,
            summary=summary,
            children=tuple(entries),
            recent_events=tuple(
                AttendanceEventSummary(
                    event_id=row.id,
                    child_profile_id=row.child_profile_id,
                    display_name=names.get(row.child_profile_id, ""),
                    event_type=row.event_type,
                    occurred_at=utc(row.occurred_at),
                    valid_until=None if row.valid_until is None else utc(row.valid_until),
                    recorded_by_caller=row.recorded_by_actor_id == principal.actor_id,
                )
                for row in history
            ),
        )

    async def get_attendance(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> ClassroomAttendance:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(session, principal, classroom_id)
            return await self._attendance_view(
                session, principal, area, facility, now or datetime.now(UTC)
            )

    async def _transition(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        child_id: UUID,
        request_id: str,
        *,
        action: str,
        lease_seconds: int,
    ) -> ClassroomAttendance:
        """Load -> lock -> decide (pure) -> append -> audit, in one transaction."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        if action != "check_out":
            try:
                validate_attendance_lease(lease_seconds)
            except ChildAttendanceError as exc:
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
            await lock_child_attendance(session, principal.tenant_id, child.id)
            # State and clock are read after the lock: those the previous writer left behind.
            now = datetime.now(UTC)
            latest = await latest_attendance_event(session, principal.tenant_id, child.id)
            current = current_attendance(
                [] if latest is None else [attendance_record(latest)], child.id, now
            )
            classroom_active = area.status == "ACTIVE" and facility.status == "ACTIVE"
            try:
                if action == "check_in":
                    transition = plan_check_in(
                        current,
                        classroom_id=area.id,
                        facility_id=facility.id,
                        child_facility_id=child.facility_id,
                        now=now,
                        lease_seconds=lease_seconds,
                        child_status=ChildStatus(child.status),
                        classroom_active=classroom_active,
                    )
                elif action == "refresh":
                    transition = plan_refresh(
                        current,
                        classroom_id=area.id,
                        now=now,
                        lease_seconds=lease_seconds,
                        child_status=ChildStatus(child.status),
                        classroom_active=classroom_active,
                    )
                else:
                    transition = plan_check_out(current, classroom_id=area.id, now=now)
            except ChildAttendanceError as exc:
                raise _clean(exc) from None
            rows = self._append(session, principal, child.id, transition)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("attendance_state_changed") from None
            if rows:
                self._audit_transition(
                    session, principal, transition, rows, request_id, lease_seconds=lease_seconds
                )
                await session.flush()
            return await self._attendance_view(session, principal, area, facility, now)

    @staticmethod
    def _append(
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        child_id: UUID,
        transition: AttendanceTransition,
    ) -> list[ChildAttendanceEvent]:
        rows = [
            ChildAttendanceEvent(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=event.facility_id,
                area_id=event.classroom_id,
                child_profile_id=child_id,
                sequence=event.sequence,
                event_type=str(event.event_type),
                source="ATTENDANCE",
                occurred_at=event.occurred_at,
                valid_until=event.valid_until,
                checked_in_at=event.checked_in_at,
                recorded_by_actor_id=principal.actor_id,
            )
            for event in transition.events
        ]
        session.add_all(rows)
        return rows

    def _audit_transition(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        transition: AttendanceTransition,
        rows: list[ChildAttendanceEvent],
        request_id: str,
        *,
        lease_seconds: int,
    ) -> None:
        """Ids, times and state transitions only: no name, reference, image or track."""
        last = rows[-1]
        metadata: dict[str, Any] = {
            "classroom_id": str(last.area_id),
            "facility_id": str(last.facility_id),
            "child_profile_id": str(last.child_profile_id),
            "source": "ATTENDANCE",
            "event_ids": [str(row.id) for row in rows],
            "previous_state": str(transition.previous.state),
        }
        if last.valid_until is not None:
            metadata["lease_seconds"] = lease_seconds
            metadata["valid_until"] = utc(last.valid_until).isoformat()
        if transition.kind is AttendanceTransitionKind.MOVED:
            metadata["from_classroom_id"] = str(rows[0].area_id)
        action = {
            AttendanceTransitionKind.CHECKED_IN: "attendance.checked_in",
            AttendanceTransitionKind.MOVED: "attendance.moved",
            AttendanceTransitionKind.REFRESHED: "attendance.refreshed",
            AttendanceTransitionKind.CHECKED_OUT: "attendance.checked_out",
        }[transition.kind]
        self._audit(
            session, principal, "child_attendance_event", last.id, action, request_id, metadata
        )

    async def check_in(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        child_id: UUID,
        request_id: str,
        *,
        lease_seconds: int = ATTENDANCE_LEASE_DEFAULT_SECONDS,
    ) -> ClassroomAttendance:
        return await self._transition(
            principal,
            classroom_id,
            child_id,
            request_id,
            action="check_in",
            lease_seconds=lease_seconds,
        )

    async def refresh(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        child_id: UUID,
        request_id: str,
        *,
        lease_seconds: int = ATTENDANCE_LEASE_DEFAULT_SECONDS,
    ) -> ClassroomAttendance:
        return await self._transition(
            principal,
            classroom_id,
            child_id,
            request_id,
            action="refresh",
            lease_seconds=lease_seconds,
        )

    async def check_out(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        child_id: UUID,
        request_id: str,
    ) -> ClassroomAttendance:
        return await self._transition(
            principal,
            classroom_id,
            child_id,
            request_id,
            action="check_out",
            lease_seconds=ATTENDANCE_LEASE_DEFAULT_SECONDS,
        )
