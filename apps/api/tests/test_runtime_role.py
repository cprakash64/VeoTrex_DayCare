"""V1-00A acceptance: the API runtime role is real, restricted, idempotently provisioned, and
Row Level Security actually constrains it.

Unit tests need no database. Database-backed tests use ``admin_settings`` (cluster admin) only
to provision, seed and inspect, and ``settings`` (the provisioned ``veotrex_api_test`` role) for
every behavioural assertion - the same boundary the deployed API runs behind.
"""

from __future__ import annotations

import base64
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from veotrex_api.config import Settings
from veotrex_api.db import (
    DatabaseRoleIdentity,
    PrivilegedDatabaseRole,
    make_engine,
    require_unprivileged,
    verify_runtime_role,
)
from veotrex_api.models import Base
from veotrex_api.runtime_role import (
    DEFAULT_TABLE_PRIVILEGES,
    RUNTIME_FUNCTION_GRANTS,
    RUNTIME_TABLE_GRANTS,
    RUNTIME_TABLES_WITHOUT_ACCESS,
    RuntimeRoleError,
    SchemaInventory,
    _normalise_signature,
    apply_plan,
    build_plan,
    inspect_schema,
    main,
    probe,
    psycopg_dsn,
    replace_credentials,
    role_attributes,
    scram_sha256_verifier,
    verify,
)

# --------------------------------------------------------------------------------------- unit


def test_every_orm_table_is_classified_exactly_once() -> None:
    """A migration that adds a table must make an explicit privilege decision for the runtime
    role. Unclassified tables would otherwise silently receive default privileges forever."""
    granted = {grant.table for grant in RUNTIME_TABLE_GRANTS}
    assert not granted & RUNTIME_TABLES_WITHOUT_ACCESS
    orm_tables = set(Base.metadata.tables) | {"alembic_version"}
    assert granted | RUNTIME_TABLES_WITHOUT_ACCESS == orm_tables


def test_runtime_never_holds_delete_on_a_tenant_table_or_writes_to_audit() -> None:
    tenant_tables = {
        name for name, table in Base.metadata.tables.items() if "tenant_id" in table.columns
    }
    for grant in RUNTIME_TABLE_GRANTS:
        if grant.table in tenant_tables:
            assert "DELETE" not in grant.privileges, grant.table
    audit = next(grant for grant in RUNTIME_TABLE_GRANTS if grant.table == "audit_events")
    assert audit.privileges == {"SELECT", "INSERT"}
    assert "DELETE" not in DEFAULT_TABLE_PRIVILEGES


def test_scram_verifier_shape_and_determinism() -> None:
    salt = b"\x01" * 16
    verifier = scram_sha256_verifier("correct horse battery", salt=salt)
    assert verifier == scram_sha256_verifier("correct horse battery", salt=salt)
    assert verifier != scram_sha256_verifier("correct horse battery", salt=b"\x02" * 16)
    scheme, rest = verifier.split("$", 1)
    assert scheme == "SCRAM-SHA-256"
    iterations_salt, keys = rest.split("$")
    iterations, encoded_salt = iterations_salt.split(":")
    assert int(iterations) == 4096
    assert base64.b64decode(encoded_salt) == salt
    stored, server = keys.split(":")
    assert len(base64.b64decode(stored)) == 32 and len(base64.b64decode(server)) == 32
    assert "correct horse" not in verifier
    for bad in ("", "tab\there", "ünïcode"):
        with pytest.raises(RuntimeRoleError):
            scram_sha256_verifier(bad)
    with pytest.raises(RuntimeRoleError):
        scram_sha256_verifier("x", iterations=1000)


def test_dsn_helpers_never_lose_the_target() -> None:
    assert psycopg_dsn("postgresql+psycopg://u:p@h:5/d") == "postgresql://u:p@h:5/d"
    assert psycopg_dsn("postgresql://u@h/d") == "postgresql://u@h/d"
    with pytest.raises(RuntimeRoleError):
        psycopg_dsn("mysql://u@h/d")
    replaced = replace_credentials(
        "postgresql+psycopg://admin:pw@127.0.0.1:55433/x_test", "r", "pw"
    )
    assert replaced == "postgresql+psycopg://r:pw@127.0.0.1:55433/x_test"


def test_signature_normalisation_drops_argument_names_only() -> None:
    assert _normalise_signature(
        "f(pending_id uuid, requested_expires_at timestamp with time zone)"
    ) == ("f(uuid, timestamp with time zone)")
    assert _normalise_signature("g()") == "g()"
    assert _normalise_signature("h(text, json)") == "h(text, json)"


def _inventory(**overrides: object) -> SchemaInventory:
    base = dict(
        database="veotrex_test",
        connected_role="veotrex_test",
        role_exists=False,
        tables=frozenset(Base.metadata.tables) | {"alembic_version"},
        functions=frozenset(RUNTIME_FUNCTION_GRANTS),
        sequences=frozenset(),
    )
    base.update(overrides)
    return SchemaInventory(**base)  # type: ignore[arg-type]


def test_plan_refuses_unmigrated_schema_and_self_owned_runtime() -> None:
    with pytest.raises(RuntimeRoleError, match="missing tables"):
        build_plan(_inventory(tables=frozenset({"tenants"})), password_verifier="x")  # noqa: S106
    with pytest.raises(RuntimeRoleError, match="missing functions"):
        build_plan(_inventory(functions=frozenset()), password_verifier="x")  # noqa: S106
    with pytest.raises(RuntimeRoleError, match="cannot also be the migration role"):
        build_plan(_inventory(), role="veotrex_test", password_verifier="x")  # noqa: S106
    with pytest.raises(RuntimeRoleError, match="password reference is required"):
        build_plan(_inventory())
    with pytest.raises(RuntimeRoleError, match="plain identifier"):
        build_plan(_inventory(), role="drop role; --", password_verifier="x")  # noqa: S106


def test_plan_renders_without_credentials_and_converges() -> None:
    plan = build_plan(
        _inventory(),
        password_verifier="SCRAM-SHA-256$4096:AA$BB:CC",  # noqa: S106 - synthetic
    )
    rendered = plan.render()
    assert "CREATE ROLE" in rendered and "NOBYPASSRLS" in rendered and "NOINHERIT" in rendered
    assert "SCRAM-SHA-256$4096" not in rendered
    assert "statement withheld" in rendered
    assert 'GRANT CONNECT ON DATABASE "veotrex_test"' in rendered
    assert 'REVOKE ALL ON SCHEMA "public"' in rendered
    assert "GRANT USAGE ON SCHEMA" in rendered and "GRANT CREATE" not in rendered
    assert 'GRANT SELECT ON TABLE "public"."tenants"' in rendered
    assert 'GRANT INSERT, SELECT ON TABLE "public"."audit_events"' in rendered
    assert 'REVOKE ALL ON TABLE "public"."ring_webhook_inbox"' in rendered
    assert (
        "ALTER DEFAULT PRIVILEGES" in rendered
        and "GRANT SELECT, INSERT, UPDATE ON TABLES" in rendered
    )
    assert "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in rendered
    assert "ALL PRIVILEGES" not in rendered and "BYPASSRLS" not in rendered.replace(
        "NOBYPASSRLS", ""
    )
    existing = build_plan(_inventory(role_exists=True))
    assert "ALTER ROLE" in existing.render() and "CREATE ROLE" not in existing.render()
    assert "PASSWORD" not in existing.render()


def test_guard_rejects_privileged_identities_without_leaking_dsn() -> None:
    require_unprivileged(DatabaseRoleIdentity("veotrex_api", False, False))
    for identity in (
        DatabaseRoleIdentity("veotrex", True, False),
        DatabaseRoleIdentity("bypass", False, True),
    ):
        with pytest.raises(PrivilegedDatabaseRole) as raised:
            require_unprivileged(identity)
        assert identity.role in str(raised.value)
        assert "postgresql" not in str(raised.value)


def test_cli_refuses_without_a_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VEOTREX_DATABASE_URL", raising=False)
    monkeypatch.setattr("veotrex_api.runtime_role._settings_database_url", lambda: None)
    assert main(["verify"]) == 2
    assert "no database URL configured" in capsys.readouterr().err
    assert main(["plan", "--url-ref", "env:MISSING_URL_FOR_TEST"]) == 2


# ---------------------------------------------------------------------------- database-backed


def _admin(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(psycopg_dsn(settings.database_url.get_secret_value()), autocommit=False)


def test_runtime_role_attributes_and_ownership(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    with _admin(admin_settings) as connection:
        attributes = role_attributes(connection, runtime_role_name)
        assert attributes.can_login
        assert attributes.restricted
        assert not attributes.superuser and not attributes.bypass_rls
        assert not attributes.create_role and not attributes.create_db
        assert not attributes.replication and not attributes.inherit
        assert verify(connection, role=runtime_role_name) == []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_auth_members WHERE member = "
                "(SELECT oid FROM pg_roles WHERE rolname = %s)",
                (runtime_role_name,),
            )
            assert (cursor.fetchone() or (1,))[0] == 0, "runtime role must belong to no role"
            cursor.execute(
                "SELECT relname, pg_get_userbyid(relowner), relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
            )
            for table, owner, rls, force in cursor.fetchall():
                assert owner != runtime_role_name, table
                if (
                    "tenant_id"
                    in Base.metadata.tables.get(str(table), Base.metadata.tables["tenants"]).columns
                    and table != "tenant_identity_bindings"
                ):
                    assert rls and force, table


def test_bootstrap_is_idempotent_and_never_touches_the_password_unasked(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    def snapshot(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[object, ...]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (runtime_role_name,)
            )
            password_hash = cursor.fetchone()
            cursor.execute(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = %s ORDER BY 1, 2",
                (runtime_role_name,),
            )
            table_grants = tuple(cursor.fetchall())
            cursor.execute(
                "SELECT routine_name FROM information_schema.role_routine_grants "
                "WHERE grantee = %s ORDER BY 1",
                (runtime_role_name,),
            )
            routine_grants = tuple(cursor.fetchall())
            cursor.execute(
                "SELECT defaclobjtype, defaclacl::text FROM pg_default_acl "
                "WHERE defaclnamespace = 'public'::regnamespace ORDER BY 1"
            )
            defaults = tuple(cursor.fetchall())
        return (password_hash, table_grants, routine_grants, defaults)

    with _admin(admin_settings) as connection:
        before = snapshot(connection)
        assert before[0] is not None and str(before[0][0]).startswith("SCRAM-SHA-256$")
        inventory = inspect_schema(connection, runtime_role_name)
        assert inventory.role_exists
        plan = build_plan(inventory, role=runtime_role_name)
        apply_plan(connection, plan)
        apply_plan(connection, plan)
        connection.commit()
        assert snapshot(connection) == before
        assert verify(connection, role=runtime_role_name) == []


def test_default_privileges_apply_to_future_tables_and_functions_of_the_migration_role(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    """The migration role creates a table and a function; the runtime role must be able to use
    the table without any GRANT and must NOT be able to execute the function."""
    with _admin(admin_settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert (cursor.fetchone() or ("",))[0] == inspect_schema(
            connection, runtime_role_name
        ).connected_role
        try:
            cursor.execute("CREATE TABLE public.zz_default_probe (id int)")
            cursor.execute(
                "CREATE FUNCTION public.zz_default_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'"
            )
            for privilege, expected in (
                ("SELECT", True),
                ("INSERT", True),
                ("UPDATE", True),
                ("DELETE", False),
            ):
                cursor.execute(
                    "SELECT has_table_privilege(%s, 'public.zz_default_probe', %s)",
                    (runtime_role_name, privilege),
                )
                assert (cursor.fetchone() or (None,))[0] is expected, privilege
            cursor.execute(
                "SELECT has_function_privilege(%s, 'public.zz_default_fn()', 'EXECUTE')",
                (runtime_role_name,),
            )
            assert (cursor.fetchone() or (True,))[0] is False
            cursor.execute(
                "SELECT has_function_privilege('public', 'public.zz_default_fn()', 'EXECUTE')"
            )
            assert (cursor.fetchone() or (True,))[0] is False
        finally:
            connection.rollback()


def test_probe_from_the_runtime_role_passes_every_check(
    settings: Settings, admin_settings: Settings
) -> None:
    migration_role = inspect_schema(_admin(admin_settings), "unused").connected_role
    with psycopg.connect(psycopg_dsn(settings.database_url.get_secret_value())) as connection:
        results = probe(connection, migration_role=migration_role)
    failed = [result for result in results if not result.passed]
    assert not failed, [(result.check, result.detail) for result in failed]
    assert len(results) >= 14


async def test_engine_guard_accepts_runtime_and_refuses_admin(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    runtime_engine = make_engine(settings)
    admin_engine = make_engine(admin_settings)
    try:
        identity = await verify_runtime_role(runtime_engine)
        assert identity.role == runtime_role_name and identity.subject_to_rls
        with pytest.raises(PrivilegedDatabaseRole):
            await verify_runtime_role(admin_engine)
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


# ----------------------------------------------------------------------------- RLS acceptance


async def _set_tenant(connection: AsyncConnection, tenant_id: UUID | None) -> None:
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": "" if tenant_id is None else str(tenant_id)},
    )


async def _seed_two_tenants(admin_settings: Settings) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_a, tenant_b, connection_a, connection_b = uuid4(), uuid4(), uuid4(), uuid4()
    engine = make_engine(admin_settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, status) VALUES "
                    "(:a, 'Isolation A', 'ACTIVE'), (:b, 'Isolation B', 'ACTIVE')"
                ),
                {"a": tenant_a, "b": tenant_b},
            )
            for tenant_id, connection_id, label in (
                (tenant_a, connection_a, "A"),
                (tenant_b, connection_b, "B"),
            ):
                await _set_tenant(connection, tenant_id)
                await connection.execute(
                    text(
                        "INSERT INTO camera_provider_connections "
                        "(id, tenant_id, name, provider_type, status, integration_state) "
                        "VALUES (:id, :tenant_id, :name, 'RING', 'ACTIVE', 'ACTIVE')"
                    ),
                    {"id": connection_id, "tenant_id": tenant_id, "name": f"Secret ring {label}"},
                )
    finally:
        await engine.dispose()
    return tenant_a, tenant_b, connection_a, connection_b


async def test_rls_matrix_for_the_runtime_role(
    settings: Settings, admin_settings: Settings
) -> None:
    tenant_a, tenant_b, connection_a, connection_b = await _seed_two_tenants(admin_settings)
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            # B. Tenant A reads A.
            async with connection.begin():
                await _set_tenant(connection, tenant_a)
                rows = (
                    await connection.execute(
                        text(
                            "SELECT id, name FROM camera_provider_connections "
                            "WHERE id IN (:a, :b) ORDER BY name"
                        ),
                        {"a": connection_a, "b": connection_b},
                    )
                ).all()
                assert [(row.id, row.name) for row in rows] == [(connection_a, "Secret ring A")]
                # C. Tenant A cannot read B, even by primary key.
                assert (
                    await connection.scalar(
                        text("SELECT name FROM camera_provider_connections WHERE id = :b"),
                        {"b": connection_b},
                    )
                    is None
                )
            # D. Tenant A cannot insert a Tenant B row.
            with pytest.raises(DBAPIError) as raised:
                async with connection.begin():
                    await _set_tenant(connection, tenant_a)
                    await connection.execute(
                        text(
                            "INSERT INTO camera_provider_connections "
                            "(id, tenant_id, name, provider_type, status, integration_state) "
                            "VALUES (:id, :tenant_id, 'Injected', 'RING', 'ACTIVE', 'ACTIVE')"
                        ),
                        {"id": uuid4(), "tenant_id": tenant_b},
                    )
            assert getattr(raised.value.orig, "sqlstate", None) == "42501"
            # E. Tenant A cannot update B: zero rows, no error, nothing changed.
            async with connection.begin():
                await _set_tenant(connection, tenant_a)
                result = await connection.execute(
                    text("UPDATE camera_provider_connections SET name = 'Tampered' WHERE id = :b"),
                    {"b": connection_b},
                )
                assert result.rowcount == 0
            # F. The runtime role holds no DELETE on tenant tables at all.
            with pytest.raises(DBAPIError) as raised:
                async with connection.begin():
                    await _set_tenant(connection, tenant_a)
                    await connection.execute(
                        text("DELETE FROM camera_provider_connections WHERE id = :b"),
                        {"b": connection_b},
                    )
            assert getattr(raised.value.orig, "sqlstate", None) == "42501"
            # G. Missing context fails closed: nothing visible, nothing writable.
            async with connection.begin():
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM camera_provider_connections WHERE id IN (:a, :b)"
                        ),
                        {"a": connection_a, "b": connection_b},
                    )
                    == 0
                )
            with pytest.raises(DBAPIError):
                async with connection.begin():
                    await connection.execute(
                        text(
                            "INSERT INTO camera_provider_connections "
                            "(id, tenant_id, name, provider_type, status, integration_state) "
                            "VALUES (:id, :tenant_id, 'No context', 'RING', 'ACTIVE', 'ACTIVE')"
                        ),
                        {"id": uuid4(), "tenant_id": tenant_a},
                    )
        # B's row is intact and still named as seeded, as seen by the admin.
        admin_engine = make_engine(admin_settings)
        try:
            async with admin_engine.begin() as admin:
                await _set_tenant(admin, tenant_b)
                assert (
                    await admin.scalar(
                        text("SELECT name FROM camera_provider_connections WHERE id = :b"),
                        {"b": connection_b},
                    )
                    == "Secret ring B"
                )
        finally:
            await admin_engine.dispose()
    finally:
        await engine.dispose()


async def test_tenant_context_never_leaks_across_pooled_transactions(
    settings: Settings, admin_settings: Settings
) -> None:
    """H. One physical backend, reused across transactions including a rolled-back one: each
    transaction starts with no tenant and sees only what it sets itself."""
    tenant_a, tenant_b, connection_a, connection_b = await _seed_two_tenants(admin_settings)
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    try:
        pids: set[int] = set()

        async def visible(connection: AsyncConnection) -> list[UUID]:
            pids.add(int(await connection.scalar(text("SELECT pg_backend_pid()")) or 0))
            return list(
                (
                    await connection.scalars(
                        text(
                            "SELECT id FROM camera_provider_connections WHERE id IN (:a, :b) "
                            "ORDER BY name"
                        ),
                        {"a": connection_a, "b": connection_b},
                    )
                ).all()
            )

        async with engine.connect() as connection:
            async with connection.begin():
                await _set_tenant(connection, tenant_a)
                assert await visible(connection) == [connection_a]
        # Committed transaction released the connection: the next one starts clean.
        async with engine.connect() as connection:
            async with connection.begin():
                assert await visible(connection) == []
            async with connection.begin():
                await _set_tenant(connection, tenant_b)
                assert await visible(connection) == [connection_b]
        # A rolled-back transaction must not keep its tenant either.
        async with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="induced"):
                async with connection.begin():
                    await _set_tenant(connection, tenant_a)
                    assert await visible(connection) == [connection_a]
                    raise RuntimeError("induced rollback")
            async with connection.begin():
                assert await visible(connection) == []
        # And a transaction that ended in a database error is rolled back and clean too.
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                async with connection.begin():
                    await _set_tenant(connection, tenant_a)
                    await connection.execute(text("SELECT * FROM ring_webhook_inbox"))
            async with connection.begin():
                assert await visible(connection) == []
        assert len(pids) == 1, "the pool must have reused one physical backend"
    finally:
        await engine.dispose()


async def test_security_definer_surface_is_exact_and_does_not_cross_tenants(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """I. The runtime role can execute exactly the exposed functions, and those functions
    return only what the caller is entitled to."""
    tenant_a, tenant_b, connection_a, connection_b = await _seed_two_tenants(admin_settings)
    account_b = f"acct-{connection_b.hex}"
    admin_engine = make_engine(admin_settings)
    try:
        async with admin_engine.begin() as admin:
            await _set_tenant(admin, tenant_b)
            await admin.execute(
                text(
                    "UPDATE camera_provider_connections SET external_account_id = :account "
                    "WHERE id = :id"
                ),
                {"account": account_b, "id": connection_b},
            )
            await admin.execute(
                text(
                    "CREATE OR REPLACE FUNCTION public.zz_privileged_probe() RETURNS int "
                    "LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
                )
            )
            await admin.execute(
                text("REVOKE ALL ON FUNCTION public.zz_privileged_probe() FROM PUBLIC")
            )
    finally:
        await admin_engine.dispose()

    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                exposed = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT p.proname FROM pg_proc p "
                                "WHERE p.pronamespace = 'public'::regnamespace "
                                "AND has_function_privilege(:role, p.oid, 'EXECUTE')"
                            ),
                            {"role": runtime_role_name},
                        )
                    ).all()
                )
                expected = {item.partition("(")[0] for item in RUNTIME_FUNCTION_GRANTS}
                assert exposed == expected
                # The webhook resolver is global by design (a webhook has no tenant yet): it
                # yields the connection's tenant, which the caller then sets as context; RLS
                # still governs what that context can read.
                resolved = (
                    await connection.execute(
                        text("SELECT * FROM resolve_ring_webhook_connection(:account)"),
                        {"account": account_b},
                    )
                ).one()
                assert (resolved.connection_id, resolved.tenant_id) == (connection_b, tenant_b)
                await _set_tenant(connection, tenant_a)
                assert (
                    await connection.scalar(
                        text("SELECT name FROM camera_provider_connections WHERE id = :b"),
                        {"b": connection_b},
                    )
                    is None
                )
            with pytest.raises(DBAPIError) as raised:
                async with connection.begin():
                    await connection.execute(text("SELECT public.zz_privileged_probe()"))
            assert getattr(raised.value.orig, "sqlstate", None) == "42501"
            for statement in (
                "SELECT pg_reload_conf()",
                "SELECT pg_read_file('/etc/hostname')",
                "ALTER ROLE CURRENT_USER WITH SUPERUSER",
                "ALTER ROLE CURRENT_USER WITH BYPASSRLS",
                "ALTER ROLE CURRENT_USER WITH CREATEROLE",
                "CREATE SCHEMA zz_runtime",
                "CREATE EXTENSION IF NOT EXISTS pgcrypto",
                "ALTER TABLE public.cameras NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY tenant_isolation ON public.cameras",
                "GRANT SELECT ON public.ring_webhook_inbox TO CURRENT_USER",
            ):
                with pytest.raises(DBAPIError) as raised:
                    async with connection.begin():
                        await connection.execute(text(statement))
                assert getattr(raised.value.orig, "sqlstate", None) == "42501", statement
    finally:
        await engine.dispose()
        cleanup = make_engine(admin_settings)
        try:
            async with cleanup.begin() as admin:
                await admin.execute(text("DROP FUNCTION IF EXISTS public.zz_privileged_probe()"))
        finally:
            await cleanup.dispose()
