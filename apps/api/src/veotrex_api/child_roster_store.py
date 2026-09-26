"""Reads of the child roster and attendance stream (V1-04D), shared by the child roster service
and the classroom ratio status. Queries only: callers have already set the tenant RLS context and
checked access, and every query also names ``tenant_id`` explicitly.

Decisions live in the pure :mod:`veotrex_api.child_attendance`. The count path hands the resolver
ids and statuses only; a child's display name never reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from veotrex_api.child_attendance import (
    AttendanceEventRecord,
    AttendanceEventType,
    ChildCountResolution,
    ChildRosterMember,
    ChildStatus,
    resolve_child_count,
)
from veotrex_api.models import ChildAttendanceEvent, ChildProfile


def roster_member(row: ChildProfile) -> ChildRosterMember:
    return ChildRosterMember(row.id, row.facility_id, ChildStatus(row.status))


def attendance_record(row: ChildAttendanceEvent) -> AttendanceEventRecord:
    return AttendanceEventRecord(
        event_id=row.id,
        child_profile_id=row.child_profile_id,
        facility_id=row.facility_id,
        classroom_id=row.area_id,
        sequence=row.sequence,
        event_type=AttendanceEventType(row.event_type),
        occurred_at=row.occurred_at,
        valid_until=row.valid_until,
        checked_in_at=row.checked_in_at,
    )


async def lock_child_attendance(session: AsyncSession, tenant_id: UUID, child_id: UUID) -> None:
    """Serialise attendance writes for one child, transaction-scoped. The unique
    ``(tenant_id, child_profile_id, sequence)`` key is the guarantee; the lock turns a would-be
    conflict into a wait, so the second writer re-reads the state the first left."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"child_attendance:{tenant_id}:{child_id}"},
    )


async def latest_attendance_event(
    session: AsyncSession, tenant_id: UUID, child_id: UUID
) -> ChildAttendanceEvent | None:
    row: ChildAttendanceEvent | None = await session.scalar(
        select(ChildAttendanceEvent)
        .where(
            ChildAttendanceEvent.tenant_id == tenant_id,
            ChildAttendanceEvent.child_profile_id == child_id,
        )
        .order_by(ChildAttendanceEvent.sequence.desc())
        .limit(1)
    )
    return row


@dataclass(frozen=True, slots=True)
class AttendanceState:
    """Every child of one facility with their single latest attendance event (or none)."""

    profiles: tuple[ChildProfile, ...]
    latest: dict[UUID, ChildAttendanceEvent]

    def members(self) -> list[ChildRosterMember]:
        return [roster_member(profile) for profile in self.profiles]

    def events(self) -> list[AttendanceEventRecord]:
        return [attendance_record(row) for row in self.latest.values()]


async def load_attendance_state(
    session: AsyncSession, tenant_id: UUID, facility_id: UUID
) -> AttendanceState:
    latest_subquery = (
        select(ChildAttendanceEvent)
        .where(
            ChildAttendanceEvent.tenant_id == ChildProfile.tenant_id,
            ChildAttendanceEvent.child_profile_id == ChildProfile.id,
        )
        .order_by(ChildAttendanceEvent.sequence.desc())
        .limit(1)
        .correlate(ChildProfile)
        .lateral("latest_child_event")
    )
    event = aliased(ChildAttendanceEvent, latest_subquery)
    rows = (
        await session.execute(
            select(ChildProfile, event)
            .outerjoin(event, true())
            .where(ChildProfile.tenant_id == tenant_id, ChildProfile.facility_id == facility_id)
            .order_by(ChildProfile.display_name, ChildProfile.id)
        )
    ).all()
    return AttendanceState(
        tuple(row[0] for row in rows),
        {row[0].id: row[1] for row in rows if row[1] is not None},
    )


async def resolve_attendance(
    session: AsyncSession,
    tenant_id: UUID,
    facility_id: UUID,
    classroom_id: UUID,
    now: datetime,
) -> ChildCountResolution:
    state = await load_attendance_state(session, tenant_id, facility_id)
    return resolve_child_count(
        classroom_id=classroom_id,
        facility_id=facility_id,
        now=now,
        members=state.members(),
        events=state.events(),
    )
