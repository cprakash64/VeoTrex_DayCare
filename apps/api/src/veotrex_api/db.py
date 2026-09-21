from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from veotrex_api.config import Settings

# Identity of the role this connection authenticated as. ``current_user`` rather than
# ``session_user`` so a ``SET ROLE`` performed earlier on a pooled connection is also caught.
ROLE_IDENTITY_SQL = text(
    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
)


@dataclass(frozen=True, slots=True)
class DatabaseRoleIdentity:
    role: str
    superuser: bool
    bypass_rls: bool

    @property
    def subject_to_rls(self) -> bool:
        return not (self.superuser or self.bypass_rls)


class PrivilegedDatabaseRole(RuntimeError):
    """The API is connected as a role that Row Level Security does not apply to.

    Tenant isolation rests on PostgreSQL RLS. A superuser or ``BYPASSRLS`` role reads every
    tenant's rows regardless of ``FORCE ROW LEVEL SECURITY``, so such a connection is a
    configuration fault, not a degraded mode. The message names the role and the offending
    attributes and never the connection string.
    """

    def __init__(self, identity: DatabaseRoleIdentity) -> None:
        attributes = [
            name
            for name, present in (
                ("SUPERUSER", identity.superuser),
                ("BYPASSRLS", identity.bypass_rls),
            )
            if present
        ]
        super().__init__(
            f"database role {identity.role!r} has {' and '.join(attributes)}; the API must run "
            "as a NOSUPERUSER NOBYPASSRLS runtime role (see veotrex-db-runtime-role)"
        )
        self.identity = identity


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql+psycopg://", 1
    )
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


async def inspect_role(connection: AsyncConnection) -> DatabaseRoleIdentity:
    """Read the connected role's RLS-relevant attributes from ``pg_roles``."""
    row = (await connection.execute(ROLE_IDENTITY_SQL)).one()
    return DatabaseRoleIdentity(str(row[0]), bool(row[1]), bool(row[2]))


def require_unprivileged(identity: DatabaseRoleIdentity) -> DatabaseRoleIdentity:
    """Fail closed: refuse any role that RLS would not constrain."""
    if not identity.subject_to_rls:
        raise PrivilegedDatabaseRole(identity)
    return identity


async def verify_runtime_role(engine: AsyncEngine) -> DatabaseRoleIdentity:
    """Connect once and prove the engine's role is subject to Row Level Security.

    Raises :class:`PrivilegedDatabaseRole` for a superuser or ``BYPASSRLS`` role. Connectivity
    errors propagate unchanged so callers can distinguish "misconfigured" from "unavailable".
    """
    async with engine.connect() as connection:
        return require_unprivileged(await inspect_role(connection))
