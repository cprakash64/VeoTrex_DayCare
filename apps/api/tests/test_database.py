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


async def test_migration_enables_rls_on_every_tenant_table(settings: Settings) -> None:
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            enabled = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relname = ANY(:tables) AND relrowsecurity AND relforcerowsecurity"
                ),
                {
                    "tables": [
                        "facilities",
                        "areas",
                        "zones",
                        "camera_provider_connections",
                        "cameras",
                        "edge_nodes",
                        "camera_assignments",
                        "actors",
                        "actor_identities",
                        "role_assignments",
                        "audit_events",
                    ]
                },
            )
            policies = await connection.scalar(
                text("SELECT count(*) FROM pg_policies WHERE policyname = 'tenant_isolation'")
            )
        assert enabled == 11
        assert policies == 11
    finally:
        await engine.dispose()


async def test_rls_fails_closed_without_tenant_context(settings: Settings) -> None:
    engine = make_engine(settings)
    tenant_id = uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE veotrex_test_runtime "
                    "NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(text("DROP OWNED BY veotrex_test_runtime"))
            await connection.execute(text("GRANT USAGE ON SCHEMA public TO veotrex_test_runtime"))
            await connection.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    "TO veotrex_test_runtime"
                )
            )
            await connection.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Test', 'ACTIVE')"),
                {"id": tenant_id},
            )
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE veotrex_test_runtime"))
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO facilities "
                        "(id, tenant_id, name, jurisdiction, timezone, status) VALUES "
                        "(:id, :tenant_id, 'Facility', 'US-AZ', 'America/Phoenix', 'ACTIVE')"
                    ),
                    {"id": uuid4(), "tenant_id": tenant_id},
                )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP OWNED BY veotrex_test_runtime"))
            await connection.execute(text("DROP ROLE IF EXISTS veotrex_test_runtime"))
        await engine.dispose()


async def test_pending_ring_table_requires_narrow_function_access(settings: Settings) -> None:
    engine = make_engine(settings)
    role = "veotrex_ring_runtime_test"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION list_ring_pending_candidates(timestamptz) TO {role}"
                )
            )
        async with engine.begin() as connection:
            assert not await connection.scalar(
                text("SELECT has_table_privilege(:role, 'ring_pending_links', 'SELECT')"),
                {"role": role},
            )
            assert await connection.scalar(
                text(
                    "SELECT has_function_privilege"
                    "(:role, 'list_ring_pending_candidates(timestamptz)', 'EXECUTE')"
                ),
                {"role": role},
            )
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            assert (
                await connection.execute(text("SELECT * FROM list_ring_pending_candidates(now())"))
            ).all() == []
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT * FROM ring_pending_links"))
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
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


async def test_composite_foreign_key_rejects_cross_tenant_parent(settings: Settings) -> None:
    engine = make_engine(settings)
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
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(first_tenant)},
            )
            await connection.execute(
                text(
                    "INSERT INTO facilities "
                    "(id, tenant_id, name, jurisdiction, timezone, status) VALUES "
                    "(:id, :tenant_id, 'Facility', 'US-AZ', 'America/Phoenix', 'ACTIVE')"
                ),
                {"id": facility_id, "tenant_id": first_tenant},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(second_tenant)},
            )
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


async def test_runtime_role_isolates_tenants_and_pool_reuse(settings: Settings) -> None:
    admin_engine = make_engine(settings)
    runtime_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0
    )
    role = "veotrex_rls_runtime_test"
    tenant_a, tenant_b = uuid4(), uuid4()
    facility_a, facility_b = uuid4(), uuid4()

    async def set_runtime_role(connection: AsyncConnection) -> None:
        await connection.execute(text(f"SET LOCAL ROLE {role}"))

    async def set_tenant(connection: AsyncConnection, tenant_id: UUID) -> None:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:tenant_a, 'Tenant A', 'ACTIVE'), (:tenant_b, 'Tenant B', 'ACTIVE')"
                ),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO facilities "
                    "(id, tenant_id, name, jurisdiction, timezone, status) VALUES "
                    "(:facility_a, :tenant_a, 'Facility A', 'US-AZ', 'America/Phoenix', 'ACTIVE'), "
                    "(:facility_b, :tenant_b, 'Facility B', 'US-AZ', 'America/Phoenix', 'ACTIVE')"
                ),
                {
                    "facility_a": facility_a,
                    "tenant_a": tenant_a,
                    "facility_b": facility_b,
                    "tenant_b": tenant_b,
                },
            )

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                await set_runtime_role(connection)
                role_state = (
                    await connection.execute(
                        text(
                            "SELECT current_user, r.rolsuper, r.rolbypassrls, "
                            "pg_get_userbyid(d.datdba) = current_user AS owns_database, "
                            "pg_get_userbyid(c.relowner) = current_user AS owns_table "
                            "FROM pg_roles r, pg_database d, pg_class c "
                            "WHERE r.rolname = current_user AND d.datname = current_database() "
                            "AND c.relname = 'facilities'"
                        )
                    )
                ).one()
                assert tuple(role_state) == (role, False, False, False, False)
                assert (await connection.scalars(text("SELECT id FROM facilities"))).all() == []
                first_backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                await set_runtime_role(connection)
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == first_backend_pid
                assert await connection.scalar(text("SELECT count(*) FROM facilities")) == 0

            async with connection.begin():
                await set_runtime_role(connection)
                await set_tenant(connection, tenant_a)
                assert (
                    await connection.scalars(text("SELECT id FROM facilities ORDER BY id"))
                ).all() == [facility_a]
                assert (
                    await connection.execute(
                        text("UPDATE facilities SET name = 'blocked' WHERE id = :id"),
                        {"id": facility_b},
                    )
                ).rowcount == 0
                assert (
                    await connection.execute(
                        text("DELETE FROM facilities WHERE id = :id"), {"id": facility_b}
                    )
                ).rowcount == 0

        async with runtime_engine.connect() as connection:
            async with connection.begin():
                await set_runtime_role(connection)
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == first_backend_pid
                assert await connection.scalar(text("SELECT count(*) FROM facilities")) == 0

            async with connection.begin():
                await set_runtime_role(connection)
                await set_tenant(connection, tenant_b)
                assert (await connection.scalars(text("SELECT id FROM facilities"))).all() == [
                    facility_b
                ]

            with pytest.raises(IntegrityError):
                async with connection.begin():
                    await set_runtime_role(connection)
                    await set_tenant(connection, tenant_a)
                    await connection.execute(
                        text(
                            "INSERT INTO areas "
                            "(id, tenant_id, facility_id, name, kind, status) VALUES "
                            "(:id, :tenant_id, :facility_id, 'Cross tenant', 'ROOM', 'ACTIVE')"
                        ),
                        {"id": uuid4(), "tenant_id": tenant_a, "facility_id": facility_b},
                    )
    finally:
        await runtime_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
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


async def test_identity_and_role_integrity_rejects_cross_tenant_links(settings: Settings) -> None:
    engine = make_engine(settings)
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
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_a)},
                )
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
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_a)},
                )
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
    settings: Settings,
) -> None:
    admin_engine = make_engine(settings)
    runtime_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0
    )
    role = "veotrex_identity_runtime_test"
    tenant_a, tenant_b = uuid4(), uuid4()
    organization_a = f"org_{uuid4().hex}"
    organization_b = f"org_{uuid4().hex}"
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION "
                    f"resolve_tenant_identity_binding(text, text, text) TO {role}"
                )
            )
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
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
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
                    await connection.execute(text(f"SET LOCAL ROLE {role}"))
                    await connection.scalar(text("SELECT count(*) FROM tenant_identity_bindings"))
    finally:
        await runtime_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin_engine.dispose()
