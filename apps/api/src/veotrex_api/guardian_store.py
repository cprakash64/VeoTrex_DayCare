"""Reads and locks for guardian contacts, child <-> contact links and release events (V1-04E).

Queries only: callers have already set the tenant RLS context and checked access, and every query
also names ``tenant_id`` explicitly. Decisions live in the pure :mod:`veotrex_api.guardian_release`.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from veotrex_api.child_attendance import ChildStatus
from veotrex_api.guardian_release import (
    ChildParty,
    ContactParty,
    GuardianStatus,
    LinkStatus,
    LinkTerms,
)
from veotrex_api.models import ChildGuardianLink, ChildProfile, GuardianContact


def child_party(row: ChildProfile) -> ChildParty:
    return ChildParty(row.id, row.facility_id, ChildStatus(row.status))


def contact_party(row: GuardianContact) -> ContactParty:
    return ContactParty(row.id, row.facility_id, GuardianStatus(row.status))


def link_terms(row: ChildGuardianLink) -> LinkTerms:
    return LinkTerms(
        link_id=row.id,
        child_profile_id=row.child_profile_id,
        guardian_contact_id=row.guardian_contact_id,
        facility_id=row.facility_id,
        status=LinkStatus(row.status),
        pickup_authorized=row.pickup_authorized,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        revision=row.revision,
    )


async def lock_guardian_link(
    session: AsyncSession, tenant_id: UUID, child_id: UUID, guardian_id: UUID
) -> None:
    """Serialise link writes for one child/contact pair, transaction-scoped, so two concurrent
    creators cannot both pass the "no ACTIVE link yet" check. The partial unique index is the
    guarantee; the lock turns the would-be conflict into a wait."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"child_guardian_link:{tenant_id}:{child_id}:{guardian_id}"},
    )


async def pair_links(
    session: AsyncSession,
    tenant_id: UUID,
    child_id: UUID,
    guardian_id: UUID,
    *,
    for_share: bool = False,
) -> list[ChildGuardianLink]:
    """Every link, active or not, between exactly one child and one contact."""
    statement = (
        select(ChildGuardianLink)
        .where(
            ChildGuardianLink.tenant_id == tenant_id,
            ChildGuardianLink.child_profile_id == child_id,
            ChildGuardianLink.guardian_contact_id == guardian_id,
        )
        .order_by(ChildGuardianLink.created_at, ChildGuardianLink.id)
    )
    if for_share:
        statement = statement.with_for_update(read=True)
    return list((await session.scalars(statement)).all())


async def links_of_children(
    session: AsyncSession, tenant_id: UUID, child_ids: Iterable[UUID]
) -> list[tuple[ChildGuardianLink, GuardianContact]]:
    ids = list(child_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(ChildGuardianLink, GuardianContact)
            .join(
                GuardianContact,
                (GuardianContact.id == ChildGuardianLink.guardian_contact_id)
                & (GuardianContact.tenant_id == ChildGuardianLink.tenant_id),
            )
            .where(
                ChildGuardianLink.tenant_id == tenant_id,
                ChildGuardianLink.child_profile_id.in_(ids),
            )
            .order_by(
                GuardianContact.display_name,
                ChildGuardianLink.created_at.desc(),
                ChildGuardianLink.id,
            )
        )
    ).all()
    return [(row[0], row[1]) for row in rows]
