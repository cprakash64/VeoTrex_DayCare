"""Facility staff roster, ratio eligibility and staff check-in/out (V1-04C).

Built on :class:`~veotrex_api.classroom_service.ClassroomService` so every operation inherits its
scoping exactly: the caller's tenant (RLS context plus explicit ``tenant_id`` predicates), one
facility at a time (READ_OPERATIONAL to read, ADMINISTER_FACILITY there to change anything), and
an unknown, other-tenant or unreadable identifier answered identically as ``not_found``.

Everything recorded here is an operator's action on an adult staff profile:

* an eligibility designation (``staff_ratio_eligibility``) - roster membership at a facility and
  whether the person *counts toward the configured classroom ratio*;
* a presence event (``staff_presence_events``) - checked in, refreshed, checked out.

No camera, recognition result, image, face or template is read or written, and a recognition
match can reach none of these methods: each requires an authenticated operator request naming
the staff profile explicitly. Decisions are made by the pure :mod:`veotrex_api.staff_presence`;
this module loads state, locks, appends and audits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.classroom_service import (
    _FREE_TEXT,
    CLASSROOM_KIND,
    ClassroomError,
    ClassroomService,
    clean_optional,
    facility_zone,
    local_period,
    utc,
)
from veotrex_api.models import (
    Area,
    Facility,
    StaffPresenceEvent,
    StaffProfile,
    StaffRatioEligibility,
)
from veotrex_api.staff_presence import (
    STAFF_LEASE_DEFAULT_SECONDS,
    STAFF_LEASE_MAX_SECONDS,
    STAFF_LEASE_MIN_SECONDS,
    PresenceTransition,
    StaffCountResolution,
    StaffPresenceError,
    TransitionKind,
    current_presence,
    plan_check_in,
    plan_check_out,
    plan_refresh,
    resolve_staff_count,
    select_eligibility,
    validate_lease,
)
from veotrex_api.staff_roster_store import (
    active_assignments,
    eligibility_terms,
    event_record,
    latest_event,
    load_roster_state,
    lock_staff_eligibility,
    lock_staff_presence,
)

NOTE_MAX = 500
# How many of a classroom's past events the presence endpoint returns. The stream is kept in
# full in the table; this only bounds one response.
STAFF_EVENT_HISTORY_LIMIT = 20
# Shown for a deleted profile that still has a (non-counting) open stay: the name was the
# operator's to remove, and the row is only there to explain the stale/ineligible numbers.
REMOVED_STAFF_LABEL = "Removed staff profile"

ROSTER_VALIDATION_CATEGORIES = frozenset(
    {
        "invalid_lease_seconds",
        "invalid_eligibility_note",
        "invalid_effective_date",
        "effective_period_inverted",
    }
)
ROSTER_CONFLICT_CATEGORIES = frozenset(
    {
        "facility_inactive",
        "facility_timezone_invalid",
        "classroom_inactive",
        "staff_not_active",
        "staff_not_assigned_to_facility",
        "staff_already_checked_in",
        "staff_checked_in_elsewhere",
        "staff_in_another_classroom",
        "staff_not_checked_in",
        "staff_presence_expired",
        "eligibility_exists",
        "eligibility_inactive",
        # Two writers raced past the lock (it cannot normally happen): the database kept one.
        "presence_state_changed",
    }
)


# --------------------------------------------------------------------------- summaries
@dataclass(frozen=True, slots=True)
class EligibilitySummary:
    eligibility_id: UUID
    facility_id: UUID
    staff_profile_id: UUID
    staff_display_name: str
    staff_status: str
    status: str
    counts_toward_ratio: bool
    note: str | None
    effective_from: datetime
    effective_until: datetime | None
    effective_from_date: date
    effective_through_date: date | None
    in_effect: bool
    revision: int
    created_at: datetime
    updated_at: datetime
    deactivated_at: datetime | None


@dataclass(frozen=True, slots=True)
class FacilityRoster:
    facility_id: UUID
    facility_name: str
    facility_timezone: str
    can_administer: bool
    assignments: tuple[EligibilitySummary, ...]


@dataclass(frozen=True, slots=True)
class EligibilityInput:
    staff_profile_id: UUID
    counts_toward_ratio: bool
    note: str | None
    # Facility-local days; from defaults to today, through is inclusive and optional.
    effective_from_date: date | None
    effective_through_date: date | None


@dataclass(frozen=True, slots=True)
class EligibilityChange:
    counts_toward_ratio: bool | None = None
    note: str | None = None
    set_note: bool = False
    effective_through_date: date | None = None
    set_effective_through: bool = False


@dataclass(frozen=True, slots=True)
class StaffPresenceEntry:
    """One adult staff member as the classroom presence card shows them."""

    staff_profile_id: UUID
    display_name: str
    staff_status: str
    on_facility_roster: bool
    counts_toward_ratio: bool
    counted: bool
    state: str  # PRESENT / STALE / NOT_CHECKED_IN
    location: str  # HERE / OTHER_CLASSROOM / OTHER_FACILITY / NONE
    other_classroom_id: UUID | None
    other_classroom_name: str | None
    checked_in_at: datetime | None
    last_event_at: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class StaffPresenceEventSummary:
    event_id: UUID
    staff_profile_id: UUID
    display_name: str
    event_type: str
    occurred_at: datetime
    valid_until: datetime | None
    recorded_by_caller: bool


@dataclass(frozen=True, slots=True)
class ClassroomStaffPresence:
    classroom_id: UUID
    facility_id: UUID
    classroom_active: bool
    presence_source_mode: str
    can_administer: bool
    evaluated_at: datetime
    summary: StaffCountResolution
    staff: tuple[StaffPresenceEntry, ...]
    recent_events: tuple[StaffPresenceEventSummary, ...]
    lease_min_seconds: int = STAFF_LEASE_MIN_SECONDS
    lease_max_seconds: int = STAFF_LEASE_MAX_SECONDS
    lease_default_seconds: int = STAFF_LEASE_DEFAULT_SECONDS


# ----------------------------------------------------------------------------- service
class StaffRosterService(ClassroomService):
    # --------------------------------------------------------------------- helpers
    @staticmethod
    async def _staff(
        session: AsyncSession,
        tenant_id: UUID,
        staff_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> StaffProfile:
        profile = await session.scalar(
            select(StaffProfile).where(
                StaffProfile.id == staff_id, StaffProfile.tenant_id == tenant_id
            )
        )
        if profile is None or (profile.status == "DELETED" and not include_deleted):
            # Unknown, other-tenant and deleted profiles are indistinguishable, as in V1-02A.
            raise ClassroomError("not_found")
        return profile

    async def _admin_facility(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, facility_id: UUID
    ) -> Facility:
        facility = await self._facility(session, principal, facility_id)
        if not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
            raise ClassroomError("access_denied")
        return facility

    @staticmethod
    def _eligibility_summary(
        row: StaffRatioEligibility, profile: StaffProfile, facility: Facility, now: datetime
    ) -> EligibilitySummary:
        zone = facility_zone(facility.timezone)
        through = (
            None
            if row.effective_until is None
            else (row.effective_until.astimezone(zone) - timedelta(days=1)).date()
        )
        return EligibilitySummary(
            eligibility_id=row.id,
            facility_id=row.facility_id,
            staff_profile_id=row.staff_profile_id,
            staff_display_name=REMOVED_STAFF_LABEL
            if profile.status == "DELETED"
            else profile.display_name,
            staff_status=profile.status,
            status=row.status,
            counts_toward_ratio=row.counts_toward_ratio,
            note=row.note,
            effective_from=utc(row.effective_from),
            effective_until=None if row.effective_until is None else utc(row.effective_until),
            effective_from_date=row.effective_from.astimezone(zone).date(),
            effective_through_date=through,
            in_effect=eligibility_terms(row).applies_at(now),
            revision=row.revision,
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            deactivated_at=None if row.deactivated_at is None else utc(row.deactivated_at),
        )

    @staticmethod
    def _eligibility_audit(row: StaffRatioEligibility) -> dict[str, Any]:
        # IDs, flags and dates only. The operator's note is free text and is not copied.
        return {
            "facility_id": str(row.facility_id),
            "staff_profile_id": str(row.staff_profile_id),
            "counts_toward_ratio": row.counts_toward_ratio,
            "effective_from": row.effective_from.isoformat(),
            "effective_until": None
            if row.effective_until is None
            else row.effective_until.isoformat(),
            "note_present": row.note is not None,
            "revision": row.revision,
        }

    async def _eligibility_row(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        facility_id: UUID,
        eligibility_id: UUID,
    ) -> StaffRatioEligibility:
        row = await session.scalar(
            select(StaffRatioEligibility).where(
                StaffRatioEligibility.id == eligibility_id,
                StaffRatioEligibility.tenant_id == tenant_id,
                StaffRatioEligibility.facility_id == facility_id,
            )
        )
        if row is None:
            raise ClassroomError("not_found")
        return row

    # ----------------------------------------------------------------- eligibility
    async def list_eligibility(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        *,
        staff_profile_id: UUID | None = None,
        now: datetime | None = None,
    ) -> FacilityRoster:
        """Every designation at the facility, active and historical, newest first per person."""
        self._require_any(principal, Permission.READ_OPERATIONAL)
        moment = now or datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._facility(session, principal, facility_id)
            statement = (
                select(StaffRatioEligibility, StaffProfile)
                .join(
                    StaffProfile,
                    (StaffProfile.id == StaffRatioEligibility.staff_profile_id)
                    & (StaffProfile.tenant_id == StaffRatioEligibility.tenant_id),
                )
                .where(
                    StaffRatioEligibility.tenant_id == principal.tenant_id,
                    StaffRatioEligibility.facility_id == facility.id,
                )
            )
            if staff_profile_id is not None:
                statement = statement.where(
                    StaffRatioEligibility.staff_profile_id == staff_profile_id
                )
            rows = (
                await session.execute(
                    statement.order_by(
                        StaffProfile.display_name,
                        StaffProfile.id,
                        StaffRatioEligibility.status,
                        StaffRatioEligibility.created_at.desc(),
                        StaffRatioEligibility.id,
                    )
                )
            ).all()
            return FacilityRoster(
                facility_id=facility.id,
                facility_name=facility.name,
                facility_timezone=facility.timezone,
                can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
                assignments=tuple(
                    self._eligibility_summary(row, profile, facility, moment)
                    for row, profile in rows
                ),
            )

    async def create_eligibility(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        payload: EligibilityInput,
        request_id: str,
    ) -> EligibilitySummary:
        """Place an ACTIVE staff profile on the facility roster, counting toward the configured
        ratio or not. One ACTIVE designation per person per facility; history is kept."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        note = clean_optional(
            payload.note, maximum=NOTE_MAX, pattern=_FREE_TEXT, category="invalid_eligibility_note"
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._admin_facility(session, principal, facility_id)
            if facility.status != "ACTIVE":
                raise ClassroomError("facility_inactive")
            zone = facility_zone(facility.timezone)
            now = datetime.now(UTC)
            begins, ends = local_period(
                payload.effective_from_date or now.astimezone(zone).date(),
                payload.effective_through_date,
                zone,
            )
            profile = await self._staff(session, principal.tenant_id, payload.staff_profile_id)
            if profile.status != "ACTIVE":
                raise ClassroomError("staff_not_active")
            await lock_staff_eligibility(session, principal.tenant_id, facility.id, profile.id)
            if await active_assignments(session, principal.tenant_id, facility.id, profile.id):
                raise ClassroomError("eligibility_exists")
            row = StaffRatioEligibility(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                staff_profile_id=profile.id,
                status="ACTIVE",
                counts_toward_ratio=payload.counts_toward_ratio,
                note=note,
                effective_from=begins,
                effective_until=ends,
                revision=1,
                created_by_actor_id=principal.actor_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("eligibility_exists") from None
            self._audit(
                session,
                principal,
                "staff_ratio_eligibility",
                row.id,
                "staff_eligibility.created",
                request_id,
                self._eligibility_audit(row),
            )
            await session.flush()
            await session.refresh(row)
            return self._eligibility_summary(row, profile, facility, now)

    async def update_eligibility(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        eligibility_id: UUID,
        change: EligibilityChange,
        request_id: str,
    ) -> EligibilitySummary:
        """Change an ACTIVE designation in place (revision + 1, before/after audited). The
        person and the facility of a designation never change; the start date does not either -
        a new period is a new designation."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        note = clean_optional(
            change.note, maximum=NOTE_MAX, pattern=_FREE_TEXT, category="invalid_eligibility_note"
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._admin_facility(session, principal, facility_id)
            zone = facility_zone(facility.timezone)
            row = await self._eligibility_row(
                session, principal.tenant_id, facility.id, eligibility_id
            )
            await lock_staff_eligibility(
                session, principal.tenant_id, facility.id, row.staff_profile_id
            )
            await session.refresh(row)
            if row.status != "ACTIVE":
                raise ClassroomError("eligibility_inactive")
            profile = await self._staff(session, principal.tenant_id, row.staff_profile_id)
            if profile.status != "ACTIVE":
                raise ClassroomError("staff_not_active")
            before = self._eligibility_audit(row)
            if change.counts_toward_ratio is not None:
                row.counts_toward_ratio = change.counts_toward_ratio
            if change.set_note:
                row.note = note
            if change.set_effective_through:
                start = row.effective_from.astimezone(zone).date()
                _, ends = local_period(start, change.effective_through_date, zone)
                row.effective_until = ends
            after = self._eligibility_audit(row)
            now = datetime.now(UTC)
            if after != before:
                row.revision += 1
                await session.flush()
                self._audit(
                    session,
                    principal,
                    "staff_ratio_eligibility",
                    row.id,
                    "staff_eligibility.updated",
                    request_id,
                    {"before": before, "after": self._eligibility_audit(row)},
                )
                await session.flush()
                await session.refresh(row)
            return self._eligibility_summary(row, profile, facility, now)

    async def deactivate_eligibility(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        eligibility_id: UUID,
        request_id: str,
    ) -> EligibilitySummary:
        """End a designation. Idempotent: a second call changes nothing and is not audited."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._admin_facility(session, principal, facility_id)
            row = await self._eligibility_row(
                session, principal.tenant_id, facility.id, eligibility_id
            )
            await lock_staff_eligibility(
                session, principal.tenant_id, facility.id, row.staff_profile_id
            )
            await session.refresh(row)
            profile = await self._staff(
                session, principal.tenant_id, row.staff_profile_id, include_deleted=True
            )
            if row.status == "ACTIVE":
                row.status = "INACTIVE"
                row.revision += 1
                row.deactivated_at = datetime.now(UTC)
                row.deactivated_by_actor_id = principal.actor_id
                await session.flush()
                self._audit(
                    session,
                    principal,
                    "staff_ratio_eligibility",
                    row.id,
                    "staff_eligibility.deactivated",
                    request_id,
                    {
                        "facility_id": str(row.facility_id),
                        "staff_profile_id": str(row.staff_profile_id),
                        "revision": row.revision,
                    },
                )
                await session.flush()
                await session.refresh(row)
            return self._eligibility_summary(row, profile, facility, datetime.now(UTC))

    # -------------------------------------------------------------------- presence
    async def _presence_view(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        area: Area,
        facility: Facility,
        now: datetime,
    ) -> ClassroomStaffPresence:
        state = await load_roster_state(session, principal.tenant_id, facility.id)
        events = state.events()
        terms = state.terms()
        summary = resolve_staff_count(
            classroom_id=area.id,
            facility_id=facility.id,
            now=now,
            members=state.members(),
            events=events,
            assignments=terms,
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
        counted = set(summary.counted_staff_ids)
        entries: list[StaffPresenceEntry] = []
        for profile in state.profiles:
            current = current_presence(events, profile.id, now)
            terms_now = select_eligibility(terms, profile.id, facility.id, now)
            here = current.in_classroom(area.id)
            if terms_now is None and not here:
                continue  # neither on this facility's roster nor in this room
            if profile.status == "DELETED" and not here:
                continue
            if here:
                location = "HERE"
            elif not current.open:
                location = "NONE"
            elif current.facility_id == facility.id:
                location = "OTHER_CLASSROOM"
            else:
                location = "OTHER_FACILITY"
            same_facility_room = location == "OTHER_CLASSROOM"
            entries.append(
                StaffPresenceEntry(
                    staff_profile_id=profile.id,
                    display_name=REMOVED_STAFF_LABEL
                    if profile.status == "DELETED"
                    else profile.display_name,
                    staff_status=profile.status,
                    on_facility_roster=terms_now is not None,
                    counts_toward_ratio=terms_now is not None and terms_now.counts_toward_ratio,
                    counted=profile.id in counted,
                    state=str(current.state),
                    location=location,
                    other_classroom_id=current.classroom_id if same_facility_room else None,
                    other_classroom_name=rooms.get(current.classroom_id)
                    if same_facility_room and current.classroom_id is not None
                    else None,
                    checked_in_at=None
                    if current.checked_in_at is None or location == "OTHER_FACILITY"
                    else utc(current.checked_in_at),
                    last_event_at=None
                    if current.last_event_at is None or location == "OTHER_FACILITY"
                    else utc(current.last_event_at),
                    valid_until=None
                    if current.valid_until is None or location == "OTHER_FACILITY"
                    else utc(current.valid_until),
                )
            )
        names = {
            profile.id: REMOVED_STAFF_LABEL if profile.status == "DELETED" else profile.display_name
            for profile in state.profiles
        }
        history = (
            await session.scalars(
                select(StaffPresenceEvent)
                .where(
                    StaffPresenceEvent.tenant_id == principal.tenant_id,
                    StaffPresenceEvent.area_id == area.id,
                )
                .order_by(
                    StaffPresenceEvent.occurred_at.desc(),
                    StaffPresenceEvent.sequence.desc(),
                    StaffPresenceEvent.id.desc(),
                )
                .limit(STAFF_EVENT_HISTORY_LIMIT)
            )
        ).all()
        return ClassroomStaffPresence(
            classroom_id=area.id,
            facility_id=facility.id,
            classroom_active=area.status == "ACTIVE",
            presence_source_mode=area.presence_source_mode,
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
            evaluated_at=now,
            summary=summary,
            staff=tuple(entries),
            recent_events=tuple(
                StaffPresenceEventSummary(
                    event_id=row.id,
                    staff_profile_id=row.staff_profile_id,
                    display_name=names.get(row.staff_profile_id, REMOVED_STAFF_LABEL),
                    event_type=row.event_type,
                    occurred_at=utc(row.occurred_at),
                    valid_until=None if row.valid_until is None else utc(row.valid_until),
                    recorded_by_caller=row.recorded_by_actor_id == principal.actor_id,
                )
                for row in history
            ),
        )

    async def get_staff_presence(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> ClassroomStaffPresence:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(session, principal, classroom_id)
            return await self._presence_view(
                session, principal, area, facility, now or datetime.now(UTC)
            )

    async def _transition(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        staff_profile_id: UUID,
        request_id: str,
        *,
        action: str,
        lease_seconds: int,
    ) -> ClassroomStaffPresence:
        """Load -> lock -> decide (pure) -> append -> audit, in one transaction."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        if action != "check_out":
            try:
                validate_lease(lease_seconds)
            except StaffPresenceError as exc:
                raise ClassroomError(exc.category) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            profile = await self._staff(
                session,
                principal.tenant_id,
                staff_profile_id,
                # Checking a person out is always safe, even after their profile was removed.
                include_deleted=action == "check_out",
            )
            await lock_staff_presence(session, principal.tenant_id, profile.id)
            # Read after the lock, and take the time after it: the state and the clock are those
            # the previous writer (if any) left behind.
            now = datetime.now(UTC)
            latest = await latest_event(session, principal.tenant_id, profile.id)
            current = current_presence(
                [] if latest is None else [event_record(latest)], profile.id, now
            )
            terms = select_eligibility(
                [
                    eligibility_terms(row)
                    for row in await active_assignments(
                        session, principal.tenant_id, facility.id, profile.id
                    )
                ],
                profile.id,
                facility.id,
                now,
            )
            classroom_active = area.status == "ACTIVE" and facility.status == "ACTIVE"
            try:
                if action == "check_in":
                    transition = plan_check_in(
                        current,
                        classroom_id=area.id,
                        facility_id=facility.id,
                        now=now,
                        lease_seconds=lease_seconds,
                        staff_active=profile.status == "ACTIVE",
                        classroom_active=classroom_active,
                        eligibility=terms,
                    )
                elif action == "refresh":
                    transition = plan_refresh(
                        current,
                        classroom_id=area.id,
                        now=now,
                        lease_seconds=lease_seconds,
                        staff_active=profile.status == "ACTIVE",
                        classroom_active=classroom_active,
                        eligibility=terms,
                    )
                else:
                    transition = plan_check_out(current, classroom_id=area.id, now=now)
            except StaffPresenceError as exc:
                raise ClassroomError(exc.category) from None
            rows = self._append(session, principal, transition)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("presence_state_changed") from None
            if rows:
                self._audit_transition(
                    session,
                    principal,
                    transition,
                    rows,
                    request_id,
                    lease_seconds=lease_seconds,
                    counts_toward_ratio=terms is not None and terms.counts_toward_ratio,
                )
                await session.flush()
            return await self._presence_view(session, principal, area, facility, now)

    @staticmethod
    def _append(
        session: AsyncSession, principal: AuthenticatedPrincipal, transition: PresenceTransition
    ) -> list[StaffPresenceEvent]:
        rows = [
            StaffPresenceEvent(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=event.facility_id,
                area_id=event.classroom_id,
                staff_profile_id=transition.previous.staff_profile_id,
                sequence=event.sequence,
                event_type=str(event.event_type),
                source="STAFF_ROSTER",
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
        transition: PresenceTransition,
        rows: list[StaffPresenceEvent],
        request_id: str,
        *,
        lease_seconds: int,
        counts_toward_ratio: bool,
    ) -> None:
        """IDs, times and state transitions only: no name, image, frame or template."""
        last = rows[-1]
        metadata: dict[str, Any] = {
            "classroom_id": str(last.area_id),
            "facility_id": str(last.facility_id),
            "staff_profile_id": str(last.staff_profile_id),
            "source": "STAFF_ROSTER",
            "event_ids": [str(row.id) for row in rows],
            "previous_state": str(transition.previous.state),
        }
        if last.valid_until is not None:
            metadata["lease_seconds"] = lease_seconds
            metadata["valid_until"] = utc(last.valid_until).isoformat()
            metadata["counts_toward_ratio"] = counts_toward_ratio
        if transition.kind is TransitionKind.MOVED:
            metadata["from_classroom_id"] = str(rows[0].area_id)
        action = {
            TransitionKind.CHECKED_IN: "staff_presence.checked_in",
            TransitionKind.MOVED: "staff_presence.moved",
            TransitionKind.REFRESHED: "staff_presence.refreshed",
            TransitionKind.CHECKED_OUT: "staff_presence.checked_out",
        }[transition.kind]
        self._audit(
            session, principal, "staff_presence_event", last.id, action, request_id, metadata
        )

    async def check_in(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        staff_profile_id: UUID,
        request_id: str,
        *,
        lease_seconds: int = STAFF_LEASE_DEFAULT_SECONDS,
    ) -> ClassroomStaffPresence:
        return await self._transition(
            principal,
            classroom_id,
            staff_profile_id,
            request_id,
            action="check_in",
            lease_seconds=lease_seconds,
        )

    async def refresh(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        staff_profile_id: UUID,
        request_id: str,
        *,
        lease_seconds: int = STAFF_LEASE_DEFAULT_SECONDS,
    ) -> ClassroomStaffPresence:
        return await self._transition(
            principal,
            classroom_id,
            staff_profile_id,
            request_id,
            action="refresh",
            lease_seconds=lease_seconds,
        )

    async def check_out(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        staff_profile_id: UUID,
        request_id: str,
    ) -> ClassroomStaffPresence:
        return await self._transition(
            principal,
            classroom_id,
            staff_profile_id,
            request_id,
            action="check_out",
            lease_seconds=STAFF_LEASE_DEFAULT_SECONDS,
        )
