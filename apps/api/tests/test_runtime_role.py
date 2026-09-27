# ruff: noqa: S608 - test SQL interpolates fixed module-level identifiers only, never input
"""V1-00A / V1-00A-R1 acceptance: the API runtime role is real, restricted, idempotently
provisioned, converges from an over-privileged state, fails closed for every new object, and
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
    PROBE_ABORTED,
    PROBE_ARTIFACT,
    RUNTIME_FUNCTION_GRANTS,
    TABLE_CLASSIFICATION,
    RuntimeRoleError,
    SchemaInventory,
    TableAccess,
    TableClassification,
    _normalise_signature,
    apply_plan,
    build_plan,
    execute_rollback_only,
    inspect_schema,
    main,
    probe,
    probe_artifacts,
    probe_identity_gate,
    psycopg_dsn,
    replace_credentials,
    role_attributes,
    scram_sha256_verifier,
    verify,
)

INSUFFICIENT_PRIVILEGE = "42501"
SYNTHETIC_VERIFIER = "SCRAM-SHA-256$4096:AA$BB:CC"

# --------------------------------------------------------------------------------------- unit


def test_every_orm_table_is_classified_exactly_once() -> None:
    """A migration that adds a table must make an explicit privilege decision for the runtime
    role. This is the CI gate: an unclassified ORM table fails here before it can ship."""
    orm_tables = set(Base.metadata.tables) | {"alembic_version"}
    assert set(TABLE_CLASSIFICATION) == orm_tables
    for table, entry in TABLE_CLASSIFICATION.items():
        assert isinstance(entry.access, TableAccess), table
        assert entry.reason, table


def test_classification_semantics_are_explicit() -> None:
    tenant_tables = {
        name for name, table in Base.metadata.tables.items() if "tenant_id" in table.columns
    }
    for table, entry in TABLE_CLASSIFICATION.items():
        if entry.access in (TableAccess.FUNCTION_ONLY, TableAccess.RUNTIME_NO_ACCESS):
            assert not entry.privileges, table
        if table in tenant_tables:
            assert "DELETE" not in entry.privileges, table
    assert TABLE_CLASSIFICATION["encrypted_credentials"].access is TableAccess.FUNCTION_ONLY
    assert TABLE_CLASSIFICATION["ring_webhook_inbox"].access is TableAccess.FUNCTION_ONLY
    assert TABLE_CLASSIFICATION["ring_pending_links"].access is TableAccess.FUNCTION_ONLY
    assert TABLE_CLASSIFICATION["tenant_identity_bindings"].access is TableAccess.FUNCTION_ONLY
    # V1-DEMO-03B: credential digests are function-only; nodes and assignments are read-only.
    assert TABLE_CLASSIFICATION["edge_node_credentials"].access is TableAccess.FUNCTION_ONLY
    assert TABLE_CLASSIFICATION["edge_nodes"].privileges == {"SELECT"}
    assert TABLE_CLASSIFICATION["camera_assignments"].privileges == {"SELECT"}
    assert TABLE_CLASSIFICATION["alembic_version"].access is TableAccess.RUNTIME_NO_ACCESS
    assert TABLE_CLASSIFICATION["audit_events"].privileges == {"SELECT", "INSERT"}
    assert TABLE_CLASSIFICATION["cameras"].privileges == {"SELECT", "INSERT", "UPDATE"}
    assert TABLE_CLASSIFICATION["tenants"].privileges == {"SELECT"}
    with pytest.raises(ValueError, match="explicit privilege set"):
        TableClassification(TableAccess.RUNTIME_WRITE)
    with pytest.raises(ValueError, match="implies exactly"):
        TableClassification(TableAccess.RUNTIME_READ, frozenset({"SELECT", "DELETE"}))
    with pytest.raises(ValueError, match="unknown privileges"):
        TableClassification(TableAccess.RUNTIME_WRITE, frozenset({"TRUNCATE"}))
    assert "vault_credential_authorized" not in " ".join(RUNTIME_FUNCTION_GRANTS)


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
        build_plan(_inventory(tables=frozenset({"tenants"})), password_verifier=SYNTHETIC_VERIFIER)
    with pytest.raises(RuntimeRoleError, match="missing functions"):
        build_plan(_inventory(functions=frozenset()), password_verifier=SYNTHETIC_VERIFIER)
    with pytest.raises(RuntimeRoleError, match="cannot also be the migration role"):
        build_plan(_inventory(), role="veotrex_test", password_verifier=SYNTHETIC_VERIFIER)
    with pytest.raises(RuntimeRoleError, match="password reference is required"):
        build_plan(_inventory())
    with pytest.raises(RuntimeRoleError, match="plain identifier"):
        build_plan(_inventory(), role="drop role; --", password_verifier=SYNTHETIC_VERIFIER)


def test_plan_never_grants_by_default_and_revokes_unclassified_objects() -> None:
    plan = build_plan(
        _inventory(
            tables=frozenset(Base.metadata.tables) | {"alembic_version", "future_sensitive_table"},
            functions=frozenset(RUNTIME_FUNCTION_GRANTS) | {"zz_future_fn()"},
            sequences=frozenset({"zz_future_seq"}),
        ),
        password_verifier=SYNTHETIC_VERIFIER,
    )
    rendered = plan.render()
    assert "CREATE ROLE" in rendered and "NOBYPASSRLS" in rendered and "NOINHERIT" in rendered
    assert "SCRAM-SHA-256$4096" not in rendered and "statement withheld" in rendered
    assert 'GRANT CONNECT ON DATABASE "veotrex_test"' in rendered
    assert "GRANT USAGE ON SCHEMA" in rendered and "GRANT CREATE" not in rendered
    assert 'GRANT SELECT ON TABLE "public"."tenants"' in rendered
    assert 'GRANT INSERT, SELECT ON TABLE "public"."audit_events"' in rendered
    assert 'REVOKE ALL ON TABLE "public"."encrypted_credentials"' in rendered
    assert 'GRANT SELECT ON TABLE "public"."encrypted_credentials"' not in rendered
    assert "UNCLASSIFIED" in rendered
    assert 'REVOKE ALL ON TABLE "public"."future_sensitive_table"' in rendered
    assert 'GRANT SELECT ON TABLE "public"."future_sensitive_table"' not in rendered
    assert 'REVOKE ALL ON FUNCTION "public"."zz_future_fn"()' in rendered
    assert 'REVOKE ALL ON SEQUENCE "public"."zz_future_seq"' in rendered
    # No ALTER DEFAULT PRIVILEGES ... GRANT anywhere: nothing is handed out automatically.
    for line in rendered.splitlines():
        if line.startswith("ALTER DEFAULT PRIVILEGES"):
            assert "GRANT" not in line, line
    assert 'REVOKE ALL ON TABLES FROM "veotrex_api"' in rendered
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "veotrex_test" REVOKE ALL ON FUNCTIONS FROM PUBLIC'
        in rendered
    )
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "veotrex_api" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC'
        in rendered
    )
    assert "ALL PRIVILEGES" not in rendered
    existing = build_plan(_inventory(role_exists=True))
    assert "ALTER ROLE" in existing.render() and "CREATE ROLE" not in existing.render()
    assert "PASSWORD" not in existing.render()


def test_guard_rejects_every_escape_hatch_without_leaking_dsn() -> None:
    require_unprivileged(DatabaseRoleIdentity("veotrex_api", False, False))
    for identity in (
        DatabaseRoleIdentity("veotrex", True, False),
        DatabaseRoleIdentity("bypass", False, True),
        DatabaseRoleIdentity("creator", False, False, schema_create=True),
        DatabaseRoleIdentity("owner", False, False, owned_relations=2),
        DatabaseRoleIdentity("member", False, False, role_memberships=1),
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


def _connect(settings: Settings) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(psycopg_dsn(settings.database_url.get_secret_value()), autocommit=False)


def _refused(connection: psycopg.Connection[tuple[object, ...]], statement: str) -> str:
    """Run ``statement`` in its own transaction; return the SQLSTATE it was refused with."""
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(statement)  # type: ignore[arg-type]
    except psycopg.Error as exc:
        return exc.sqlstate or "unknown"
    return "succeeded"


def _restore_production_model(
    admin: psycopg.Connection[tuple[object, ...]], runtime_role_name: str
) -> None:
    apply_plan(admin, build_plan(inspect_schema(admin, runtime_role_name), role=runtime_role_name))
    admin.commit()


def test_runtime_role_attributes_ownership_and_membership(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    with _connect(admin_settings) as connection:
        attributes = role_attributes(connection, runtime_role_name)
        assert attributes.can_login and attributes.restricted
        assert verify(connection, role=runtime_role_name) == []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_auth_members WHERE member = "
                "(SELECT oid FROM pg_roles WHERE rolname = %s)",
                (runtime_role_name,),
            )
            assert (cursor.fetchone() or (1,))[0] == 0
            cursor.execute(
                "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (runtime_role_name,)
            )
            assert (cursor.fetchone() or (True,))[0] is False
            cursor.execute(
                "SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
                "AND pg_get_userbyid(c.relowner) = %s",
                (runtime_role_name,),
            )
            assert (cursor.fetchone() or (1,))[0] == 0


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
                "SELECT defaclnamespace, defaclobjtype, defaclacl::text FROM pg_default_acl "
                "ORDER BY 1, 2"
            )
            defaults = tuple(cursor.fetchall())
        return (password_hash, table_grants, routine_grants, defaults)

    with _connect(admin_settings) as connection:
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


def test_apply_converges_an_over_privileged_role_down_to_the_allow_list(
    admin_settings: Settings,
) -> None:
    """An old deployment may carry broad grants and the V1-00A table default. apply must strip
    them, including direct CRUD on encrypted_credentials, not merely add what is missing."""
    role = "veotrex_api_overpriv_test"
    with _connect(admin_settings) as connection:
        owner = inspect_schema(connection, role).connected_role
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cursor.fetchone():
                cursor.execute(f"DROP OWNED BY {role}")
                cursor.execute(f"DROP ROLE {role}")
            cursor.execute(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS")
            cursor.execute(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {role}")
            cursor.execute(f"GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO {role}")
            cursor.execute(f"GRANT CREATE ON SCHEMA public TO {role}")
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE ON TABLES TO {role}"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                f"GRANT USAGE ON SEQUENCES TO {role}"
            )
        try:
            problems = verify(connection, role=role)
            assert any("encrypted_credentials: DELETE is granted" in p for p in problems)
            assert any("can CREATE" in p for p in problems)
            assert any("default privileges grant the runtime role" in p for p in problems)
            assert any("vault_credential_authorized" in p and "granted" in p for p in problems)
            plan = build_plan(inspect_schema(connection, role), role=role)
            apply_plan(connection, plan)
            connection.commit()
            assert verify(connection, role=role) == []
            with connection.cursor() as cursor:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cursor.execute(
                        "SELECT has_table_privilege(%s, 'public.encrypted_credentials', %s)",
                        (role, privilege),
                    )
                    assert (cursor.fetchone() or (True,))[0] is False, privilege
                cursor.execute(
                    "SELECT count(*) FROM pg_default_acl WHERE defaclacl::text LIKE %s "
                    "AND pg_get_userbyid(defaclrole) <> %s",
                    (f"%{role}=%", role),
                )
                assert (cursor.fetchone() or (1,))[0] == 0
        finally:
            connection.rollback()
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(f"DROP OWNED BY {role}")
                cursor.execute(f"DROP ROLE IF EXISTS {role}")
            connection.commit()


def test_future_table_is_inaccessible_until_classified(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """Mandatory fail-closed check: a table the migration role creates is unreachable to the
    runtime for every privilege, and becomes reachable only for the privileges a deliberate
    classification grants."""
    table = "future_sensitive_table"
    with _connect(admin_settings) as admin:
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS public.{table}")
            cursor.execute(f"CREATE TABLE public.{table} (id int PRIMARY KEY, secret text)")
            cursor.execute(f"INSERT INTO public.{table} VALUES (1, 'synthetic')")
        try:
            with _connect(settings) as runtime:
                for statement in (
                    f"SELECT * FROM public.{table}",
                    f"INSERT INTO public.{table} VALUES (2, 'x')",
                    f"UPDATE public.{table} SET secret = 'y'",
                    f"DELETE FROM public.{table}",
                ):
                    assert _refused(runtime, statement) == INSUFFICIENT_PRIVILEGE, statement
            problems = verify(admin, role=runtime_role_name)
            assert f"table {table}: unclassified" in problems
            # Deliberate classification: read-only. Only SELECT appears.
            classified = dict(TABLE_CLASSIFICATION)
            classified[table] = TableClassification(
                TableAccess.RUNTIME_READ, frozenset({"SELECT"}), "test"
            )
            plan = build_plan(
                inspect_schema(admin, runtime_role_name),
                role=runtime_role_name,
                classification=classified,
            )
            apply_plan(admin, plan)
            admin.commit()
            with _connect(settings) as runtime:
                with runtime.transaction(), runtime.cursor() as cursor:
                    cursor.execute(f"SELECT count(*) FROM public.{table}")
                    assert (cursor.fetchone() or (0,))[0] == 1
                for statement in (
                    f"INSERT INTO public.{table} VALUES (3, 'x')",
                    f"UPDATE public.{table} SET secret = 'y'",
                    f"DELETE FROM public.{table}",
                ):
                    assert _refused(runtime, statement) == INSUFFICIENT_PRIVILEGE, statement
            assert verify(admin, role=runtime_role_name, classification=classified) == []
        finally:
            with admin.transaction(), admin.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS public.{table}")
            _restore_production_model(admin, runtime_role_name)


def test_future_function_is_inaccessible_until_allowed(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    function = "zz_future_fn"
    with _connect(admin_settings) as admin:
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute(
                f"CREATE OR REPLACE FUNCTION public.{function}() RETURNS int "
                "LANGUAGE sql SECURITY DEFINER AS 'SELECT 42'"
            )
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    f"SELECT has_function_privilege('public', 'public.{function}()', 'EXECUTE')"
                )
                assert (cursor.fetchone() or (True,))[0] is False, "PUBLIC must not execute"
                cursor.execute(
                    f"SELECT has_function_privilege(%s, 'public.{function}()', 'EXECUTE')",
                    (runtime_role_name,),
                )
                assert (cursor.fetchone() or (True,))[0] is False, "runtime must not execute"
            with _connect(settings) as runtime:
                assert _refused(runtime, f"SELECT public.{function}()") == INSUFFICIENT_PRIVILEGE
            plan = build_plan(
                inspect_schema(admin, runtime_role_name),
                role=runtime_role_name,
                function_grants=(*RUNTIME_FUNCTION_GRANTS, f"{function}()"),
            )
            apply_plan(admin, plan)
            admin.commit()
            with _connect(settings) as runtime, runtime.transaction(), runtime.cursor() as cursor:
                cursor.execute(f"SELECT public.{function}()")
                assert (cursor.fetchone() or (0,))[0] == 42
        finally:
            with admin.transaction(), admin.cursor() as cursor:
                cursor.execute(f"DROP FUNCTION IF EXISTS public.{function}()")
            _restore_production_model(admin, runtime_role_name)


def test_future_sequence_is_inaccessible_until_allowed(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    sequence = "zz_future_seq"
    with _connect(admin_settings) as admin:
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute(f"CREATE SEQUENCE public.{sequence}")
        try:
            with admin.cursor() as cursor:
                for privilege in ("USAGE", "SELECT", "UPDATE"):
                    cursor.execute(
                        f"SELECT has_sequence_privilege('public', 'public.{sequence}', %s)",
                        (privilege,),
                    )
                    assert (cursor.fetchone() or (True,))[0] is False, privilege
                    cursor.execute(
                        f"SELECT has_sequence_privilege(%s, 'public.{sequence}', %s)",
                        (runtime_role_name, privilege),
                    )
                    assert (cursor.fetchone() or (True,))[0] is False, privilege
            with _connect(settings) as runtime:
                assert (
                    _refused(runtime, f"SELECT nextval('public.{sequence}')")
                    == INSUFFICIENT_PRIVILEGE
                )
                assert (
                    _refused(runtime, f"SELECT last_value FROM public.{sequence}")
                    == INSUFFICIENT_PRIVILEGE
                )
            # The production plan has no sequence allow list yet: apply keeps it revoked.
            _restore_production_model(admin, runtime_role_name)
            assert verify(admin, role=runtime_role_name) == []
            with _connect(settings) as runtime:
                assert (
                    _refused(runtime, f"SELECT nextval('public.{sequence}')")
                    == INSUFFICIENT_PRIVILEGE
                )
        finally:
            with admin.transaction(), admin.cursor() as cursor:
                cursor.execute(f"DROP SEQUENCE IF EXISTS public.{sequence}")


def test_verify_refuses_to_infer_the_migration_role_from_a_runtime_connection(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """V1-00A-PROD-R1: production ran verify from the API container, connected as the runtime
    role and without --migration-role, and evaluated the migration-role invariants against
    veotrex_api. verify must refuse to guess in that situation and must pass when told."""
    with _connect(settings) as runtime:
        with pytest.raises(RuntimeRoleError, match="connected as the runtime role"):
            verify(runtime, role=runtime_role_name)
    with _connect(admin_settings) as admin:
        migration_role = inspect_schema(admin, "unused").connected_role
    with _connect(settings) as runtime:
        assert verify(runtime, role=runtime_role_name, migration_role=migration_role) == []


def test_apply_converges_the_runtime_roles_own_function_default(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    """V1-00A-PROD-R1 convergence: reproduce the production state (correct grants, but the
    runtime role's own creator defaults left at PostgreSQL's PUBLIC EXECUTE), prove verify
    reports exactly that, then prove apply removes it without touching anything else."""

    def snapshot(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[object, ...]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
                "rolinherit, rolcanlogin FROM pg_roles WHERE rolname = %s",
                (runtime_role_name,),
            )
            attributes = cursor.fetchone()
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
                "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' "
                "ORDER BY 1, 2"
            )
            policies = tuple(cursor.fetchall())
            cursor.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY 1"
            )
            rls = tuple(cursor.fetchall())
            cursor.execute(
                "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (runtime_role_name,)
            )
            create = cursor.fetchone()
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cursor.execute(
                    "SELECT has_table_privilege(%s, 'public.encrypted_credentials', %s)",
                    (runtime_role_name, privilege),
                )
                assert (cursor.fetchone() or (True,))[0] is False, privilege
        return (attributes, table_grants, routine_grants, policies, rls, create)

    def own_function_default(connection: psycopg.Connection[tuple[object, ...]]) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.defaclacl::text FROM pg_default_acl d JOIN pg_roles r "
                "ON r.oid = d.defaclrole WHERE r.rolname = %s AND d.defaclnamespace = 0 "
                "AND d.defaclobjtype = 'f'",
                (runtime_role_name,),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    with _connect(admin_settings) as admin:
        migration_role = inspect_schema(admin, "unused").connected_role
        # Production-equivalent state: grants correct, own function default back to PUBLIC.
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {runtime_role_name} "
                "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            )
        admin.commit()
        assert own_function_default(admin) is None, "PUBLIC default must be the built-in one"
        before = snapshot(admin)
        problems = verify(admin, role=runtime_role_name, migration_role=migration_role)
        assert problems == [
            f"functions created by the runtime role {runtime_role_name} would default to "
            "PUBLIC EXECUTE"
        ]
        apply_plan(
            admin,
            build_plan(
                inspect_schema(admin, runtime_role_name),
                role=runtime_role_name,
                migration_role=migration_role,
            ),
        )
        admin.commit()
        assert verify(admin, role=runtime_role_name, migration_role=migration_role) == []
        after_default = own_function_default(admin)
        assert after_default is not None and "{=" not in after_default and ",=" not in after_default
        assert snapshot(admin) == before


def test_fresh_provision_hardens_own_function_default_and_it_holds_when_creating(
    admin_settings: Settings,
) -> None:
    """V1-00A-PROD-R1 fresh provision: a brand-new runtime role gets its own function default
    hardened. To prove the default is effective, the test grants CREATE in an ISOLATED schema
    (never public), creates a function AS the role, and checks that neither PUBLIC nor an
    unrelated role may execute it. Everything the test created is removed afterwards."""
    role = "veotrex_api_fresh_test"
    other = "veotrex_other_fresh_test"
    schema = "zz_runtime_owned_test"
    password = "fresh-test-only-" + uuid4().hex
    with _connect(admin_settings) as admin:
        migration_role = inspect_schema(admin, "unused").connected_role
        with admin.transaction(), admin.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            for name in (role, other):
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
                if cursor.fetchone():
                    cursor.execute(f"DROP OWNED BY {name}")
                    cursor.execute(f"DROP ROLE {name}")
            cursor.execute(f"CREATE ROLE {other} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        admin.commit()
        try:
            plan = build_plan(
                inspect_schema(admin, role),
                role=role,
                migration_role=migration_role,
                password_verifier=scram_sha256_verifier(password),
            )
            apply_plan(admin, plan)
            admin.commit()
            assert verify(admin, role=role, migration_role=migration_role) == []
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT d.defaclacl::text FROM pg_default_acl d JOIN pg_roles r "
                    "ON r.oid = d.defaclrole WHERE r.rolname = %s AND d.defaclnamespace = 0 "
                    "AND d.defaclobjtype = 'f'",
                    (role,),
                )
                acl = str((cursor.fetchone() or ("<none>",))[0])
            assert acl != "<none>" and "{=" not in acl and ",=" not in acl
            # Isolated schema with CREATE for the test only; public stays untouched.
            with admin.transaction(), admin.cursor() as cursor:
                cursor.execute(f"CREATE SCHEMA {schema}")
                cursor.execute(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {role}")
                cursor.execute(f"GRANT USAGE ON SCHEMA {schema} TO {other}")
            admin.commit()
            runtime_url = replace_credentials(
                admin_settings.database_url.get_secret_value(), role, password
            )
            with psycopg.connect(psycopg_dsn(runtime_url)) as runtime:
                with runtime.transaction(), runtime.cursor() as cursor:
                    cursor.execute(
                        f"CREATE FUNCTION {schema}.zz_created_by_runtime() RETURNS int "
                        "LANGUAGE sql AS 'SELECT 7'"
                    )
                assert (
                    _refused(
                        runtime,
                        "CREATE FUNCTION public.zz_never() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                    )
                    == INSUFFICIENT_PRIVILEGE
                )
            created = f"{schema}.zz_created_by_runtime()"
            with admin.cursor() as cursor:
                cursor.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (created,))
                assert (cursor.fetchone() or (True,))[0] is False, "PUBLIC must not execute"
                cursor.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (other, created))
                assert (cursor.fetchone() or (True,))[0] is False, "unrelated role must not execute"
                cursor.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, created))
                assert (cursor.fetchone() or (False,))[0] is True, "the owner keeps EXECUTE"
                cursor.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,))
                assert (cursor.fetchone() or (True,))[0] is False
        finally:
            admin.rollback()
            with admin.transaction(), admin.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                for name in (role, other):
                    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
                    if cursor.fetchone():
                        cursor.execute(f"DROP OWNED BY {name}")
                        cursor.execute(f"DROP ROLE {name}")
            admin.commit()


def test_probe_from_the_runtime_role_passes_every_check(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    with _connect(admin_settings) as admin:
        migration_role = inspect_schema(admin, "unused").connected_role
    with _connect(settings) as connection:
        results = probe(connection, role=runtime_role_name, migration_role=migration_role)
    failed = [result for result in results if not result.passed]
    assert not failed, [(result.check, result.detail) for result in failed]
    # V1-01A-2-R1: identity gate, artifact guard, then the active checks (21 in total);
    # V1-DEMO-03B adds the two edge-credential checks.
    assert len(results) == 23
    assert results[0].check == "runtime role identity" and results[1].check.startswith("no prior")
    assert not any(result.check == PROBE_ABORTED for result in results)
    databases = [result for result in results if result.check == "cannot create databases"]
    assert databases[0].detail.startswith("catalog:"), "never executed"


async def test_engine_guard_accepts_runtime_and_refuses_admin(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    runtime_engine = make_engine(settings)
    admin_engine = make_engine(admin_settings)
    try:
        identity = await verify_runtime_role(runtime_engine)
        assert identity.role == runtime_role_name and identity.subject_to_rls
        assert identity.violations == ()
        with pytest.raises(PrivilegedDatabaseRole):
            await verify_runtime_role(admin_engine)
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


def test_every_runtime_executable_function_is_a_safe_security_definer(
    admin_settings: Settings, runtime_role_name: str
) -> None:
    """Section 11: fixed search_path, no dynamic SQL, no PUBLIC EXECUTE, expected owner, and
    runtime EXECUTE exactly where the allow list says."""
    expected = {_normalise_signature(item) for item in RUNTIME_FUNCTION_GRANTS}
    with _connect(admin_settings) as admin, admin.cursor() as cursor:
        cursor.execute("SELECT current_user")
        owner = str((cursor.fetchone() or ("",))[0])
        cursor.execute(
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
            "pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig::text, p.prosrc, "
            "has_function_privilege(%s, p.oid, 'EXECUTE'), "
            "has_function_privilege('public', p.oid, 'EXECUTE'), l.lanname "
            "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
            "WHERE p.pronamespace = 'public'::regnamespace",
            (runtime_role_name,),
        )
        rows = cursor.fetchall()
    assert rows, "no application functions found"
    seen: set[str] = set()
    for row in rows:
        (
            signature,
            function_owner,
            secdef,
            config,
            source,
            runtime_execute,
            public_execute,
            language,
        ) = row
        normalised = _normalise_signature(str(signature))
        seen.add(normalised)
        assert function_owner == owner, signature
        assert secdef is True, signature
        assert config is not None and "search_path=pg_catalog, public" in str(config), signature
        assert public_execute is False, signature
        assert runtime_execute is (normalised in expected), signature
        lowered = str(source).lower()
        if language == "plpgsql":
            assert "execute " not in lowered and "format(" not in lowered, signature
        assert "set role" not in lowered and "row_security" not in lowered, signature
    assert expected <= seen


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
            # B. Tenant A reads A, and only its own tenant row.
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
                tenant_rows = (
                    await connection.scalars(
                        text("SELECT id FROM tenants WHERE id IN (:a, :b)"),
                        {"a": tenant_a, "b": tenant_b},
                    )
                ).all()
                assert tenant_rows == [tenant_a]
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
            assert getattr(raised.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
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
            assert getattr(raised.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
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
                assert await connection.scalar(text("SELECT count(*) FROM tenants")) == 0
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
        async with engine.connect() as connection:
            async with connection.begin():
                assert await visible(connection) == []
            async with connection.begin():
                await _set_tenant(connection, tenant_b)
                assert await visible(connection) == [connection_b]
        async with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="induced"):
                async with connection.begin():
                    await _set_tenant(connection, tenant_a)
                    assert await visible(connection) == [connection_a]
                    raise RuntimeError("induced rollback")
            async with connection.begin():
                assert await visible(connection) == []
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
            for statement in (
                "SELECT public.vault_credential_authorized"
                "('ring_pending_link', gen_random_uuid(), 'open')",
                "SELECT pg_reload_conf()",
                "SELECT pg_read_file('/etc/hostname')",
                "ALTER ROLE CURRENT_USER WITH SUPERUSER",
                "ALTER ROLE CURRENT_USER WITH BYPASSRLS",
                "ALTER ROLE CURRENT_USER WITH CREATEROLE",
                "CREATE SCHEMA zz_runtime",
                "CREATE EXTENSION IF NOT EXISTS pgcrypto",
                "ALTER TABLE public.cameras NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY tenant_isolation ON public.cameras",
                "DROP POLICY tenant_self ON public.tenants",
                "GRANT SELECT ON public.ring_webhook_inbox TO CURRENT_USER",
                "GRANT SELECT ON public.encrypted_credentials TO CURRENT_USER",
            ):
                with pytest.raises(DBAPIError) as raised:
                    async with connection.begin():
                        await connection.execute(text(statement))
                assert getattr(raised.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE, (
                    statement
                )
    finally:
        await engine.dispose()


# ------------------------------------------------------------- V1-01A-2-R1: probe fails closed


def _security_snapshot(admin: psycopg.Connection[tuple[object, ...]]) -> tuple[object, ...]:
    """cameras RLS flags, owner, tenant_isolation USING/WITH CHECK, audit row count,
    and the three probe artifacts. Everything the incident changed, in one tuple."""
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'cameras'"
        )
        rls = cursor.fetchone()
        cursor.execute(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE tablename = 'cameras' AND policyname = 'tenant_isolation'"
        )
        policy = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM public.audit_events")
        audits = cursor.fetchone()
        cursor.execute(
            "SELECT (SELECT count(*) FROM pg_class WHERE relname = %s), "
            "(SELECT count(*) FROM pg_roles WHERE rolname = %s), "
            "(SELECT count(*) FROM pg_database WHERE datname = %s)",
            (PROBE_ARTIFACT, PROBE_ARTIFACT, PROBE_ARTIFACT),
        )
        artifacts = cursor.fetchone()
    admin.rollback()
    return (rls, policy, audits, artifacts)


def _seed_audit_sentinel(admin: psycopg.Connection[tuple[object, ...]]) -> UUID:
    tenant_id, sentinel = uuid4(), uuid4()
    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO tenants (id, name, status) VALUES (%s, %s, 'ACTIVE')",
            (tenant_id, f"Probe sentinel {tenant_id.hex[:8]}"),
        )
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cursor.execute(
            "INSERT INTO audit_events (id, tenant_id, actor_id, action, target_type, "
            "target_id, request_id, metadata) VALUES (%s, %s, NULL, 'probe.sentinel', "
            "'test', NULL, 'probe-sentinel', '{}'::json)",
            (sentinel, tenant_id),
        )
    admin.commit()
    return sentinel


def _audit_row_exists(admin: psycopg.Connection[tuple[object, ...]], sentinel: UUID) -> bool:
    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM public.audit_events WHERE id = %s", (sentinel,))
        row = cursor.fetchone()
    admin.rollback()
    return bool(row and int(str(row[0])) == 1)


def test_probe_with_the_admin_identity_aborts_before_any_mutation(
    admin_settings: Settings, runtime_role_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator mistake of V1-01A-2, reproduced: probe over the ADMIN DSN. It must report
    the identity failure, abort, and change nothing - no artifacts, no RLS/policy/owner change,
    no audit deletion."""
    with _connect(admin_settings) as admin:
        sentinel = _seed_audit_sentinel(admin)
        before = _security_snapshot(admin)
        assert before[3] == (0, 0, 0), "test cluster must start without probe artifacts"

        results = probe(admin, role=runtime_role_name, migration_role="anything")

        assert [result.check for result in results] == ["runtime role identity", PROBE_ABORTED]
        assert results[0].passed is False and results[1].passed is False
        assert f"expected={runtime_role_name}" in results[0].detail
        assert "superuser=True" in results[0].detail or "createdb=True" in results[0].detail
        assert "0 active checks run" in results[1].detail
        assert _security_snapshot(admin) == before
        assert _audit_row_exists(admin, sentinel)

        # The console entry point behaves the same and exits non-zero.
        admin_url = admin_settings.database_url.get_secret_value()
        import os

        os.environ["VEOTREX_PROBE_TEST_ADMIN_URL"] = admin_url
        try:
            code = main(
                [
                    "probe",
                    "--role",
                    runtime_role_name,
                    "--migration-role",
                    "anything",
                    "--url-ref",
                    "env:VEOTREX_PROBE_TEST_ADMIN_URL",
                ]
            )
        finally:
            os.environ.pop("VEOTREX_PROBE_TEST_ADMIN_URL", None)
        captured = capsys.readouterr()
        assert code == 1
        assert f"FAIL  {PROBE_ABORTED}" in captured.out
        assert captured.out.count("\n") == 2, "exactly the gate line and the abort line"
        assert "never the admin one" in captured.err
        assert admin_url not in captured.out + captured.err
        assert _security_snapshot(admin) == before
        assert _audit_row_exists(admin, sentinel)


def test_probe_with_a_restricted_but_differently_named_role_aborts(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """Wrong identity alone is sufficient: the real restricted test role, asked to prove it is
    the production role name, stops at the gate even though every attribute is restricted."""
    assert runtime_role_name != "veotrex_api"
    with _connect(admin_settings) as admin:
        before = _security_snapshot(admin)
    with _connect(settings) as connection:
        results = probe(connection, role="veotrex_api", migration_role="anything")
    assert [result.check for result in results] == ["runtime role identity", PROBE_ABORTED]
    assert "superuser=False" in results[0].detail and "expected=veotrex_api" in results[0].detail
    with _connect(admin_settings) as admin:
        assert _security_snapshot(admin) == before


def test_probe_stops_when_prior_artifacts_exist_and_never_drops_them(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    with _connect(admin_settings) as admin:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE TABLE public.{PROBE_ARTIFACT} (id int)")
        admin.commit()
        try:
            assert probe_artifacts(admin).passed is False
            with _connect(settings) as connection:
                results = probe(connection, role=runtime_role_name)
            assert [result.check for result in results] == [
                "runtime role identity",
                "no prior probe artifacts",
                PROBE_ABORTED,
            ]
            assert results[0].passed and not results[1].passed
            assert "table=1" in results[1].detail
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_class WHERE relname = %s", (PROBE_ARTIFACT,)
                )
                assert cursor.fetchone() == (1,), "the probe must not clean up"
            admin.rollback()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS public.{PROBE_ARTIFACT}")
            admin.commit()
    assert probe_artifacts(admin if not admin.closed else _connect(admin_settings)).passed


def test_probe_never_persists_an_audit_delete_even_when_delete_is_granted(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    """Mandatory acceptance: DELETE accidentally granted, sentinel row present, identity
    otherwise correct. The check must FAIL and the sentinel must survive."""
    with _connect(admin_settings) as admin:
        sentinel = _seed_audit_sentinel(admin)
        with admin.cursor() as cursor:
            cursor.execute(f'GRANT DELETE ON public.audit_events TO "{runtime_role_name}"')
        admin.commit()
        try:
            with _connect(settings) as connection:
                results = probe(connection, role=runtime_role_name)
            by_check = {result.check: result for result in results}
            assert by_check["cannot delete audit rows"].passed is False
            assert (
                by_check["cannot delete audit rows"].detail == "statement succeeded (rolled back)"
            )
            assert by_check["runtime role identity"].passed
            assert PROBE_ABORTED not in by_check
            assert _audit_row_exists(admin, sentinel), "the rollback must preserve every row"
        finally:
            with admin.cursor() as cursor:
                cursor.execute(f'REVOKE DELETE ON public.audit_events FROM "{runtime_role_name}"')
            admin.commit()
        with _connect(settings) as connection:
            assert all(result.passed for result in probe(connection, role=runtime_role_name))


def test_rollback_only_executor_never_persists_rls_policy_or_ownership_changes(
    admin_settings: Settings,
) -> None:
    """The probe's own executor, driven with an identity that CAN perform the mutation: each
    forbidden statement succeeds (FAIL outcome) and every security control is unchanged."""
    with _connect(admin_settings) as admin:
        before = _security_snapshot(admin)
        with admin.cursor() as cursor:
            cursor.execute("SELECT current_user")
            me = str((cursor.fetchone() or ("",))[0])
        admin.rollback()
        for statements in (
            ("ALTER TABLE public.cameras DISABLE ROW LEVEL SECURITY",),
            ("ALTER TABLE public.cameras NO FORCE ROW LEVEL SECURITY",),
            ("ALTER POLICY tenant_isolation ON public.cameras USING (true)",),
            ("ALTER POLICY tenant_isolation ON public.cameras WITH CHECK (true)",),
            ("ALTER TABLE public.cameras OWNER TO CURRENT_USER",),
            ("DELETE FROM public.audit_events",),
            (f"CREATE TABLE public.{PROBE_ARTIFACT} (id int)",),
            (f"CREATE ROLE {PROBE_ARTIFACT} NOLOGIN",),
            (f'SET LOCAL ROLE "{me}"',),
            ("SET LOCAL row_security = off", "SELECT count(*) FROM public.cameras"),
        ):
            assert execute_rollback_only(admin, *statements) == ("succeeded", None), statements
            assert _security_snapshot(admin) == before, statements
        # A refusal is classified by SQLSTATE and also leaves nothing behind.
        assert execute_rollback_only(admin, "SELECT * FROM public.does_not_exist") == (
            "refused",
            "42P01",
        )
        assert _security_snapshot(admin) == before
        # The connection is usable and not left inside a transaction or a switched role.
        with admin.cursor() as cursor:
            cursor.execute("SELECT current_user, txid_current_if_assigned()")
            row = cursor.fetchone()
        admin.rollback()
        assert row is not None and row[0] == me and row[1] is None


def test_identity_gate_is_read_only_and_names_every_invariant(
    settings: Settings, admin_settings: Settings, runtime_role_name: str
) -> None:
    with _connect(settings) as connection:
        gate = probe_identity_gate(connection, role=runtime_role_name)
    assert gate.passed
    for key in (
        f"current_user={runtime_role_name}",
        f"expected={runtime_role_name}",
        "superuser=False",
        "bypassrls=False",
        "createrole=False",
        "createdb=False",
        "replication=False",
        "inherit=False",
        "schema_create=False",
        "owned_relations=0",
        "role_memberships=0",
    ):
        assert key in gate.detail, key
    with _connect(admin_settings) as admin:
        assert probe_identity_gate(admin, role=runtime_role_name).passed is False
