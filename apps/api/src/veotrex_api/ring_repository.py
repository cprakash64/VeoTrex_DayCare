from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PendingLinkState(StrEnum):
    RECEIVED = "RECEIVED"
    UNCLAIMED = "UNCLAIMED"
    CLAIMING = "CLAIMING"
    RING_CONFIRMATION_UNCERTAIN = "RING_CONFIRMATION_UNCERTAIN"
    RING_CONFIRMED_UNBOUND = "RING_CONFIRMED_UNBOUND"
    CLAIMED = "CLAIMED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class ConnectionState(StrEnum):
    CONFIGURING = "CONFIGURING"
    ACTIVE = "ACTIVE"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    REFRESH_UNCERTAIN = "REFRESH_UNCERTAIN"
    DISCONNECTED = "DISCONNECTED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    id: UUID
    ring_account_id: str
    secret_ref: str
    generation: int
    access_expires_at: datetime


class RingPendingRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        pending_id: UUID,
        secret_ref: str,
        generation: int,
        access_expires_at: datetime,
    ) -> None:
        await session.execute(
            text(
                "SELECT create_ring_pending_link"
                "(:pending_id, :secret_ref, :generation, :expires_at)"
            ),
            {
                "pending_id": pending_id,
                "secret_ref": secret_ref,
                "generation": generation,
                "expires_at": access_expires_at,
            },
        )

    async def complete_account(
        self, session: AsyncSession, pending_id: UUID, account_id: str
    ) -> bool:
        result = await session.scalar(
            text("SELECT complete_ring_pending_account(:pending_id, :account_id)"),
            {"pending_id": pending_id, "account_id": account_id},
        )
        return result is True

    async def candidates(
        self, session: AsyncSession, received_after: datetime
    ) -> tuple[PendingCandidate, ...]:
        rows = (
            await session.execute(
                text("SELECT * FROM list_ring_pending_candidates(:received_after)"),
                {"received_after": received_after},
            )
        ).all()
        return tuple(
            PendingCandidate(
                id=row.id,
                ring_account_id=row.ring_account_id,
                secret_ref=row.credential_secret_ref,
                generation=row.credential_generation,
                access_expires_at=row.access_expires_at,
            )
            for row in rows
        )

    async def record_failure(
        self, session: AsyncSession, pending_id: UUID, failure_category: str
    ) -> bool:
        result = await session.scalar(
            text("SELECT record_ring_pending_failure(:pending_id, :failure)"),
            {"pending_id": pending_id, "failure": failure_category},
        )
        return result is True

    async def start_claim(
        self, session: AsyncSession, pending_id: UUID, tenant_id: UUID, actor_id: UUID
    ) -> bool:
        result = await session.scalar(
            text("SELECT start_ring_pending_claim(:pending_id, :tenant_id, :actor_id)"),
            {"pending_id": pending_id, "tenant_id": tenant_id, "actor_id": actor_id},
        )
        return result is True

    async def transition(
        self,
        session: AsyncSession,
        pending_id: UUID,
        expected: PendingLinkState,
        requested: PendingLinkState,
        failure_category: str | None = None,
    ) -> bool:
        result = await session.scalar(
            text(
                "SELECT transition_ring_pending_link(:pending_id, :expected, :requested, :failure)"
            ),
            {
                "pending_id": pending_id,
                "expected": expected.value,
                "requested": requested.value,
                "failure": failure_category,
            },
        )
        return result is True
