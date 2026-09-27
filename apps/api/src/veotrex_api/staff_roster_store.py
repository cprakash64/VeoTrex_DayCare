"""Reads of the staff roster and presence stream (V1-04C), shared by the roster service and the
classroom ratio status. Queries only: every caller has already set the tenant RLS context and
checked the principal's access, and every query also names ``tenant_id`` explicitly.

The decisions themselves live in the pure :mod:`veotrex_api.staff_presence`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from veotrex_api.models import StaffPresenceEvent, StaffProfile, StaffRatioEligibility
from veotrex_api.staff_presence import (
    EligibilityTerms,
    RosterMember,
    StaffCountResolution,
    StaffPresenceEventRecord,
    StaffPresenceEventType,
    resolve_staff_count,
)


def eligibility_terms(row: StaffRatioEligibility) -> EligibilityTerms:
    return EligibilityTerms(
        assignment_id=row.id,
        staff_profile_id=row.staff_profile_id,
        facility_id=row.facility_id,
        counts_toward_ratio=row.counts_toward_ratio,
        active=row.status == "ACTIVE",
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        revision=row.revision,
    )


def event_record(row: StaffPresenceEvent) -> StaffPresenceEventRecord:
    return StaffPresenceEventRecord(
        event_id=row.id,
        staff_profile_id=row.staff_profile_id,
        facility_id=row.facility_id,
        classroom_id=row.area_id,
        sequence=row.sequence,
        event_type=StaffPresenceEventType(row.event_type),
        occurred_at=row.occurred_at,
        valid_until=row.valid_until,
        checked_in_at=row.checked_in_at,
    )


async def lock_staff_presence(session: AsyncSession, tenant_id: UUID, staff_id: UUID) -> None:
    """Serialise presence writes for one person, transaction-scoped. The unique
    ``(tenant_id, staff_profile_id, sequence)`` key is the guarantee; this lock only turns a
    would-be conflict into a wait, so the second writer re-reads the state the first left."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"staff_presence:{tenant_id}:{staff_id}"},
    )


async def lock_staff_eligibility(
    session: AsyncSession, tenant_id: UUID, facility_id: UUID, staff_id: UUID
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"staff_eligibility:{tenant_id}:{facility_id}:{staff_id}"},
    )


async def latest_event(
    session: AsyncSession, tenant_id: UUID, staff_id: UUID
) -> StaffPresenceEvent | None:
    row: StaffPresenceEvent | None = await session.scalar(
        select(StaffPresenceEvent)
        .where(
            StaffPresenceEvent.tenant_id == tenant_id,
            StaffPresenceEvent.staff_profile_id == staff_id,
        )
        .order_by(StaffPresenceEvent.sequence.desc())
        .limit(1)
    )
    return row


async def active_assignments(
    session: AsyncSession,
    tenant_id: UUID,
    facility_id: UUID,
    staff_id: UUID | None = None,
) -> list[StaffRatioEligibility]:
    statement = select(StaffRatioEligibility).where(
        StaffRatioEligibility.tenant_id == tenant_id,
        StaffRatioEligibility.facility_id == facility_id,
        StaffRatioEligibility.status == "ACTIVE",
    )
    if staff_id is not None:
        statement = statement.where(StaffRatioEligibility.staff_profile_id == staff_id)
    return list((await session.scalars(statement.order_by(StaffRatioEligibility.id))).all())


@dataclass(frozen=True, slots=True)
class RosterState:
    """Every staff profile of the tenant with its single latest presence event (or none), and
    the ACTIVE designations at one facility. Bounded by the tenant's staff limit."""

    profiles: tuple[StaffProfile, ...]
    latest: dict[UUID, StaffPresenceEvent]
    assignments: tuple[StaffRatioEligibility, ...]

    def members(self) -> list[RosterMember]:
        return [RosterMember(profile.id, profile.status == "ACTIVE") for profile in self.profiles]

    def events(self) -> list[StaffPresenceEventRecord]:
        return [event_record(row) for row in self.latest.values()]

    def terms(self) -> list[EligibilityTerms]:
        return [eligibility_terms(row) for row in self.assignments]


async def load_roster_state(
    session: AsyncSession, tenant_id: UUID, facility_id: UUID
) -> RosterState:
    latest_subquery = (
        select(StaffPresenceEvent)
        .where(
            StaffPresenceEvent.tenant_id == StaffProfile.tenant_id,
            StaffPresenceEvent.staff_profile_id == StaffProfile.id,
        )
        .order_by(StaffPresenceEvent.sequence.desc())
        .limit(1)
        .correlate(StaffProfile)
        .lateral("latest_staff_event")
    )
    event = aliased(StaffPresenceEvent, latest_subquery)
    rows = (
        await session.execute(
            select(StaffProfile, event)
            .outerjoin(event, true())
            .where(StaffProfile.tenant_id == tenant_id)
            .order_by(StaffProfile.display_name, StaffProfile.id)
        )
    ).all()
    profiles = tuple(row[0] for row in rows)
    latest = {row[0].id: row[1] for row in rows if row[1] is not None}
    assignments = tuple(await active_assignments(session, tenant_id, facility_id))
    return RosterState(profiles, latest, assignments)


async def resolve_roster(
    session: AsyncSession,
    tenant_id: UUID,
    facility_id: UUID,
    classroom_id: UUID,
    now: datetime,
) -> StaffCountResolution:
    state = await load_roster_state(session, tenant_id, facility_id)
    return resolve_staff_count(
        classroom_id=classroom_id,
        facility_id=facility_id,
        now=now,
        members=state.members(),
        events=state.events(),
        assignments=state.terms(),
    )
