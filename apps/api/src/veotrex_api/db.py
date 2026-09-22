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
# Every column is a catalog fact readable by any role: attributes, whether the role could create
# objects in the application schema, how many application relations it owns, and whether it
# belongs to any other role (through which privileges could be recovered).
ROLE_IDENTITY_SQL = text(
    "SELECT r.rolname, r.rolsuper, r.rolbypassrls, "
    "has_schema_privilege(current_user, 'public', 'CREATE'), "
    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm') "
    " AND c.relowner = r.oid), "
    "(SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) "
    "FROM pg_roles r WHERE r.rolname = current_user"
)


@dataclass(frozen=True, slots=True)
class DatabaseRoleIdentity:
    role: str
    superuser: bool
    bypass_rls: bool
    schema_create: bool = False
    owned_relations: int = 0
    role_memberships: int = 0

    @property
    def subject_to_rls(self) -> bool:
        return not (self.superuser or self.bypass_rls)

    @property
    def violations(self) -> tuple[str, ...]:
        """Human-readable names of every invariant this identity breaks."""
        found: list[str] = []
        if self.superuser:
            found.append("SUPERUSER")
        if self.bypass_rls:
            found.append("BYPASSRLS")
        if self.schema_create:
            found.append("CREATE on the application schema")
        if self.owned_relations:
            found.append(f"ownership of {self.owned_relations} application relation(s)")
        if self.role_memberships:
            found.append(f"membership in {self.role_memberships} other role(s)")
        return tuple(found)


class PrivilegedDatabaseRole(RuntimeError):
    """The API is connected as a role that Row Level Security does not apply to.

    Tenant isolation rests on PostgreSQL RLS. A superuser or ``BYPASSRLS`` role reads every
    tenant's rows regardless of ``FORCE ROW LEVEL SECURITY``, so such a connection is a
    configuration fault, not a degraded mode. The message names the role and the offending
    attributes and never the connection string.
    """

    def __init__(self, identity: DatabaseRoleIdentity) -> None:
        super().__init__(
            f"database role {identity.role!r} has {' and '.join(identity.violations)}; the API "
            "must run as a NOSUPERUSER NOBYPASSRLS runtime role that owns nothing, cannot CREATE "
            "in the schema and belongs to no other role (see veotrex-db-runtime-role)"
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
    return DatabaseRoleIdentity(
        str(row[0]),
        bool(row[1]),
        bool(row[2]),
        bool(row[3]),
        int(row[4] or 0),
        int(row[5] or 0),
    )


def require_unprivileged(identity: DatabaseRoleIdentity) -> DatabaseRoleIdentity:
    """Fail closed: refuse any role RLS would not constrain, or that could escape the model
    through object ownership, schema CREATE, or membership in another role."""
    if identity.violations:
        raise PrivilegedDatabaseRole(identity)
    return identity


async def verify_runtime_role(engine: AsyncEngine) -> DatabaseRoleIdentity:
    """Connect once and prove the engine's role is subject to Row Level Security.

    Raises :class:`PrivilegedDatabaseRole` for a superuser or ``BYPASSRLS`` role. Connectivity
    errors propagate unchanged so callers can distinguish "misconfigured" from "unavailable".
    """
    async with engine.connect() as connection:
        return require_unprivileged(await inspect_role(connection))
