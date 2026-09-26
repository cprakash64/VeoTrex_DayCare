"""Schema and Row Level Security behaviour across the real runtime boundary.

``settings`` is the restricted runtime role the API connects as; ``admin_settings`` is the cluster
admin used only to seed fixtures and read function-only tables. Every RLS assertion below is
made from the runtime role's own connection, never from a superuser pretending with SET ROLE.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine

from veotrex_api.config import Settings
from veotrex_api.db import make_engine, make_session_factory, session_scope
from veotrex_api.tenant_context import (
    apply_tenant_to_transaction,
    bind_tenant,
    reset_tenant,
)

TENANT_TABLES = [
    "facilities",
    "areas",
    "zones",
    "camera_provider_connections",
    "cameras",
    "camera_provider_devices",
    "camera_provider_components",
    "provider_events",
    "edge_nodes",
    "camera_assignments",
    "actors",
    "actor_identities",
    "role_assignments",
    "audit_events",
    "staff_profiles",
    "staff_enrollment_images",
    "staff_face_templates",
    # V1-DEMO-03B
    "edge_node_credentials",
    # V1-04A
    "classroom_ratio_policies",
    # V1-04B
    "classroom_presence_snapshots",
    # V1-04C
    "staff_ratio_eligibility",
    "staff_presence_events",
]


async def set_tenant(connection: AsyncConnection, tenant_id: UUID) -> None:
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def test_database_connection(settings: Settings) -> None:
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


async def test_session_factory_scope_connects_and_closes(settings: Settings) -> None:
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        scope = session_scope(factory)
        session = await anext(scope)
        assert await session.scalar(text("SELECT 1")) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(scope)
    finally:
        await engine.dispose()


async def test_migration_enables_rls_on_every_tenant_table(admin_settings: Settings) -> None:
    engine = make_engine(admin_settings)
    try:
        async with engine.connect() as connection:
            enabled = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relname = ANY(:tables) AND relrowsecurity AND relforcerowsecurity"
                ),
                {"tables": TENANT_TABLES},
            )
            policies = await connection.scalar(
                text("SELECT count(*) FROM pg_policies WHERE policyname = 'tenant_isolation'")
            )
        assert enabled == len(TENANT_TABLES) == 22
        assert policies == 22
        async with engine.connect() as connection:
            tenants_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'tenants'"
                    )
                )
            ).one()
            assert tuple(tenants_rls) == (True, True)
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM pg_policies WHERE policyname = 'tenant_self'")
                )
                == 1
            )
    finally:
        await engine.dispose()


async def test_tenants_table_exposes_only_the_current_tenant(
    settings: Settings, admin_settings: Settings
) -> None:
    """The runtime holds SELECT on tenants, but the self policy makes it a single-row view."""
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    tenant_a, tenant_b = uuid4(), uuid4()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:a, 'Self A', 'ACTIVE'), (:b, 'Self B', 'ACTIVE')"
                ),
                {"a": tenant_a, "b": tenant_b},
            )
        async with engine.connect() as connection:
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM tenants")) == 0
            async with connection.begin():
                await set_tenant(connection, tenant_a)
                names = (await connection.scalars(text("SELECT name FROM tenants"))).all()
                assert names == ["Self A"]
                assert (
                    await connection.scalar(
                        text("SELECT name FROM tenants WHERE id = :b"), {"b": tenant_b}
                    )
                    is None
                )
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_rls_fails_closed_without_tenant_context(
    settings: Settings, admin_settings: Settings
) -> None:
    """With no ``app.tenant_id`` the policy's predicate is NULL: nothing is visible and nothing
    can be written, even for a tenant that exists."""
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    tenant_id = uuid4()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Test', 'ACTIVE')"),
                {"id": tenant_id},
            )
            await set_tenant(connection, tenant_id)
            await connection.execute(
                text(
                    "INSERT INTO camera_provider_connections "
                    "(id, tenant_id, name, provider_type, status, integration_state) "
                    "VALUES (:id, :tenant_id, 'Closed', 'RING', 'ACTIVE', 'ACTIVE')"
                ),
                {"id": uuid4(), "tenant_id": tenant_id},
            )
        async with engine.connect() as connection:
            async with connection.begin():
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM camera_provider_connections "
                            "WHERE tenant_id = :tenant_id"
                        ),
                        {"tenant_id": tenant_id},
                    )
                    == 0
                )
            with pytest.raises(DBAPIError):
                async with connection.begin():
                    await connection.execute(
                        text(
                            "INSERT INTO camera_provider_connections "
                            "(id, tenant_id, name, provider_type, status, integration_state) "
                            "VALUES (:id, :tenant_id, 'Blocked', 'RING', 'ACTIVE', 'ACTIVE')"
                        ),
                        {"id": uuid4(), "tenant_id": tenant_id},
                    )
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_pending_ring_table_requires_narrow_function_access(
    settings: Settings, runtime_role_name: str
) -> None:
    engine = make_engine(settings)
    try:
        async with engine.begin() as connection:
            assert not await connection.scalar(
                text("SELECT has_table_privilege(:role, 'ring_pending_links', 'SELECT')"),
                {"role": runtime_role_name},
            )
            assert await connection.scalar(
                text(
                    "SELECT has_function_privilege"
                    "(:role, 'list_ring_pending_candidates(timestamptz)', 'EXECUTE')"
                ),
                {"role": runtime_role_name},
            )
            assert (
                await connection.execute(text("SELECT * FROM list_ring_pending_candidates(now())"))
            ).all() == []
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT * FROM ring_pending_links"))
    finally:
        await engine.dispose()


async def test_webhook_inbox_is_global_but_function_only(
    settings: Settings, runtime_role_name: str
) -> None:
    engine = make_engine(settings)
    try:
        async with engine.begin() as connection:
            assert not await connection.scalar(
                text("SELECT has_table_privilege(:role, 'ring_webhook_inbox', 'SELECT')"),
                {"role": runtime_role_name},
            )
            assert await connection.scalar(
                text(
                    "SELECT ingest_ring_webhook(:id, :request, '1.1', now(), 'account', "
                    "'event', 'future_event', NULL, NULL, NULL, NULL, '[]'::json, '[]'::json)"
                ),
                {"id": uuid4(), "request": f"runtime-{uuid4()}"},
            )
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT * FROM ring_webhook_inbox"))
    finally:
        await engine.dispose()


async def test_pending_candidate_query_excludes_expired_and_archived(settings: Settings) -> None:
    engine = make_engine(settings)
    active, expired, archived = uuid4(), uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            now = datetime.now(UTC)
            for pending_id, account, expiry in (
                (active, f"active-{active.hex}", now + timedelta(hours=1)),
                (expired, f"expired-{expired.hex}", now - timedelta(seconds=1)),
                (archived, f"archived-{archived.hex}", now + timedelta(hours=1)),
            ):
                await connection.execute(
                    text("SELECT create_ring_pending_link(:id, :ref, 1, :expires)"),
                    {
                        "id": pending_id,
                        "ref": f"vault://test/{pending_id}",
                        "expires": expiry,
                    },
                )
                assert await connection.scalar(
                    text("SELECT complete_ring_pending_account(:id, :account)"),
                    {"id": pending_id, "account": account},
                )
            assert await connection.scalar(
                text("SELECT transition_ring_pending_link(:id, 'UNCLAIMED', 'ARCHIVED', NULL)"),
                {"id": archived},
            )
            rows = (
                await connection.execute(
                    text("SELECT id FROM list_ring_pending_candidates(now() - interval '1 hour')")
                )
            ).all()
            ids = {row.id for row in rows}
            assert active in ids
            assert expired not in ids
            assert archived not in ids
    finally:
        await engine.dispose()


async def test_composite_foreign_key_rejects_cross_tenant_parent(
    admin_settings: Settings,
) -> None:
    engine = make_engine(admin_settings)
    first_tenant, second_tenant, facility_id = uuid4(), uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:first, 'First', 'ACTIVE'), (:second, 'Second', 'ACTIVE')"
                ),
                {"first": first_tenant, "second": second_tenant},
            )
            await set_tenant(connection, first_tenant)
            await connection.execute(
                text(
                    "INSERT INTO facilities "
                    "(id, tenant_id, name, jurisdiction, timezone, status) VALUES "
                    "(:id, :tenant_id, 'Facility', 'US-AZ', 'America/Phoenix', 'ACTIVE')"
                ),
                {"id": facility_id, "tenant_id": first_tenant},
            )
            await set_tenant(connection, second_tenant)
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO areas (id, tenant_id, facility_id, name, kind, status) "
                        "VALUES (:id, :tenant_id, :facility_id, 'Cross tenant', 'ROOM', 'ACTIVE')"
                    ),
                    {
                        "id": uuid4(),
                        "tenant_id": second_tenant,
                        "facility_id": facility_id,
                    },
                )
    finally:
        await engine.dispose()


async def test_runtime_role_isolates_tenants_and_pool_reuse(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """The real runtime role, over one pooled backend connection reused across transactions."""
    admin_engine = make_engine(admin_settings)
    runtime_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0
    )
    tenant_a, tenant_b = uuid4(), uuid4()
    camera_a, camera_b = uuid4(), uuid4()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:tenant_a, 'Tenant A', 'ACTIVE'), (:tenant_b, 'Tenant B', 'ACTIVE')"
                ),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            )
            await set_tenant(connection, tenant_a)
            await connection.execute(
                text(
                    "INSERT INTO camera_provider_connections "
                    "(id, tenant_id, name, provider_type, status, integration_state) "
                    "VALUES (:id, :tenant_id, 'Camera A', 'RING', 'ACTIVE', 'ACTIVE')"
                ),
                {"id": camera_a, "tenant_id": tenant_a},
            )
            await set_tenant(connection, tenant_b)
            await connection.execute(
                text(
                    "INSERT INTO camera_provider_connections "
                    "(id, tenant_id, name, provider_type, status, integration_state) "
                    "VALUES (:id, :tenant_id, 'Camera B', 'RING', 'ACTIVE', 'ACTIVE')"
                ),
                {"id": camera_b, "tenant_id": tenant_b},
            )

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                role_state = (
                    await connection.execute(
                        text(
                            "SELECT current_user, r.rolsuper, r.rolbypassrls, "
                            "pg_get_userbyid(d.datdba) = current_user AS owns_database, "
                            "pg_get_userbyid(c.relowner) = current_user AS owns_table "
                            "FROM pg_roles r, pg_database d, pg_class c "
                            "WHERE r.rolname = current_user AND d.datname = current_database() "
                            "AND c.relname = 'cameras'"
                        )
                    )
                ).one()
                assert tuple(role_state) == (runtime_role_name, False, False, False, False)
                assert (
                    await connection.scalars(
                        text("SELECT id FROM camera_provider_connections WHERE id IN (:a, :b)"),
                        {"a": camera_a, "b": camera_b},
                    )
                ).all() == []
                first_backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == first_backend_pid
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM camera_provider_connections WHERE id IN (:a, :b)"
                        ),
                        {"a": camera_a, "b": camera_b},
                    )
                    == 0
                )

            async with connection.begin():
                await set_tenant(connection, tenant_a)
                assert (
                    await connection.scalars(
                        text("SELECT id FROM camera_provider_connections WHERE id IN (:a, :b)"),
                        {"a": camera_a, "b": camera_b},
                    )
                ).all() == [camera_a]
                assert (
                    await connection.execute(
                        text(
                            "UPDATE camera_provider_connections SET name = 'blocked' WHERE id = :id"
                        ),
                        {"id": camera_b},
                    )
                ).rowcount == 0

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == first_backend_pid
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM camera_provider_connections WHERE id IN (:a, :b)"
                        ),
                        {"a": camera_a, "b": camera_b},
                    )
                    == 0
                )

            async with connection.begin():
                await set_tenant(connection, tenant_b)
                assert (
                    await connection.scalars(
                        text("SELECT id FROM camera_provider_connections WHERE id IN (:a, :b)"),
                        {"a": camera_a, "b": camera_b},
                    )
                ).all() == [camera_b]

            with pytest.raises(DBAPIError):
                async with connection.begin():
                    await set_tenant(connection, tenant_a)
                    await connection.execute(
                        text(
                            "INSERT INTO camera_provider_connections "
                            "(id, tenant_id, name, provider_type, status, integration_state) "
                            "VALUES (:id, :tenant_id, 'Cross', 'RING', 'ACTIVE', 'ACTIVE')"
                        ),
                        {"id": uuid4(), "tenant_id": tenant_b},
                    )
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


async def test_tenant_context_helper_is_transaction_local(settings: Settings) -> None:
    engine = make_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid4()
    try:
        async with factory() as session:
            with pytest.raises(RuntimeError, match="tenant context is required"):
                await apply_tenant_to_transaction(session)

            token = bind_tenant(tenant_id)
            try:
                async with session.begin():
                    assert await apply_tenant_to_transaction(session) == tenant_id
                    assert await session.scalar(
                        text("SELECT current_setting('app.tenant_id')")
                    ) == str(tenant_id)
            finally:
                reset_tenant(token)

            async with session.begin():
                assert (
                    await session.scalar(
                        text("SELECT NULLIF(current_setting('app.tenant_id', true), '')")
                    )
                    is None
                )
    finally:
        await engine.dispose()


async def test_identity_and_role_integrity_rejects_cross_tenant_links(
    admin_settings: Settings,
) -> None:
    engine = make_engine(admin_settings)
    tenant_a, tenant_b = uuid4(), uuid4()
    actor_a, actor_b, facility_b = uuid4(), uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:tenant_a, 'Identity A', 'ACTIVE'), (:tenant_b, 'Identity B', 'ACTIVE')"
                ),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) VALUES "
                    "(:actor_a, :tenant_a, 'Actor A', 'ACTIVE'), "
                    "(:actor_b, :tenant_b, 'Actor B', 'ACTIVE')"
                ),
                {
                    "actor_a": actor_a,
                    "tenant_a": tenant_a,
                    "actor_b": actor_b,
                    "tenant_b": tenant_b,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO facilities "
                    "(id, tenant_id, name, jurisdiction, timezone, status) VALUES "
                    "(:id, :tenant_id, 'Facility B', 'US-AZ', 'America/Phoenix', 'ACTIVE')"
                ),
                {"id": facility_b, "tenant_id": tenant_b},
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await set_tenant(connection, tenant_a)
                await connection.execute(
                    text(
                        "INSERT INTO actor_identities "
                        "(id, tenant_id, actor_id, provider, issuer, subject) VALUES "
                        "(:id, :tenant_a, :actor_b, 'auth0', 'https://idp.example/', 'subject')"
                    ),
                    {"id": uuid4(), "tenant_a": tenant_a, "actor_b": actor_b},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await set_tenant(connection, tenant_a)
                await connection.execute(
                    text(
                        "INSERT INTO role_assignments "
                        "(id, tenant_id, actor_id, role, facility_id) VALUES "
                        "(:id, :tenant_a, :actor_a, 'FACILITY_ADMIN', :facility_b)"
                    ),
                    {
                        "id": uuid4(),
                        "tenant_a": tenant_a,
                        "actor_a": actor_a,
                        "facility_b": facility_b,
                    },
                )
    finally:
        await engine.dispose()


async def test_external_organization_resolution_is_exact_and_rls_fails_closed(
    settings: Settings, admin_settings: Settings
) -> None:
    admin_engine = make_engine(admin_settings)
    runtime_engine = make_engine(settings)
    tenant_a, tenant_b = uuid4(), uuid4()
    organization_a = f"org_{uuid4().hex}"
    organization_b = f"org_{uuid4().hex}"
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:tenant_a, 'Resolver A', 'ACTIVE'), (:tenant_b, 'Resolver B', 'ACTIVE')"
                ),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_identity_bindings "
                    "(id, tenant_id, provider, issuer, external_organization_id) VALUES "
                    "(:id_a, :tenant_a, 'auth0', 'https://idp.example/', :org_a), "
                    "(:id_b, :tenant_b, 'auth0', 'https://idp.example/', :org_b)"
                ),
                {
                    "id_a": uuid4(),
                    "tenant_a": tenant_a,
                    "org_a": organization_a,
                    "id_b": uuid4(),
                    "tenant_b": tenant_b,
                    "org_b": organization_b,
                },
            )

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                assert (
                    await connection.scalar(
                        text(
                            "SELECT resolve_tenant_identity_binding"
                            "('auth0', 'https://idp.example/', :organization)"
                        ),
                        {"organization": organization_a},
                    )
                    == tenant_a
                )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT resolve_tenant_identity_binding"
                            "('auth0', 'https://idp.example/', :organization)"
                        ),
                        {"organization": organization_a.upper()},
                    )
                    is None
                )
            with pytest.raises(DBAPIError):
                async with connection.begin():
                    await connection.scalar(text("SELECT count(*) FROM tenant_identity_bindings"))
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()
