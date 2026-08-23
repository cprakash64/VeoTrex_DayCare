from contextvars import ContextVar, Token
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)


def bind_tenant(tenant_id: UUID) -> Token[UUID | None]:
    return _tenant_id.set(tenant_id)


def reset_tenant(token: Token[UUID | None]) -> None:
    _tenant_id.reset(token)


async def apply_tenant_to_transaction(session: AsyncSession) -> UUID:
    """Apply the tenant to PostgreSQL RLS for the current transaction; fail closed."""
    tenant_id = _tenant_id.get()
    if tenant_id is None:
        raise RuntimeError("tenant context is required for tenant-owned data access")
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    return tenant_id
