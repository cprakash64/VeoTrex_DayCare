"""PostgreSQL runtime-role provisioning for the VeoTrex API (V1-00A).

Row Level Security is only a tenant boundary when the connecting role is subject to it. A
superuser, or any role with ``BYPASSRLS``, reads every tenant's rows regardless of ``FORCE ROW
LEVEL SECURITY``. This module provisions the restricted role the API must connect as, applies the
smallest privilege set the current code needs, and installs default privileges so tables created
by future migrations are usable without a developer remembering a ``GRANT``.

Three identities are involved and deliberately kept apart:

``bootstrap / admin``
    The cluster's bootstrap superuser (``POSTGRES_USER``). Runs this module. Never the API.
``migration``
    The role that owns the schema and executes Alembic. Today it is the same role as the
    bootstrap identity; default privileges are keyed to it because PostgreSQL scopes
    ``ALTER DEFAULT PRIVILEGES`` to the *creating* role, not to the schema.
``runtime``
    ``LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT``, owns
    nothing, cannot ``CREATE`` in the schema, and holds only the table and function privileges
    enumerated here. The API connects as this role.

Role provisioning is cluster-level administration, not an application schema change, so it lives
here behind a privileged console script rather than in an Alembic migration. Running it twice is
a no-op: attributes are re-asserted, grants are revoked-then-granted so they converge exactly, and
the password is touched only when a password reference is supplied explicitly.

No password, DSN or secret value is ever printed, logged or included in an exception. The
password is sent to PostgreSQL as a pre-computed SCRAM-SHA-256 verifier so that the plaintext
never appears in server logs or ``pg_stat_statements`` either.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

DEFAULT_RUNTIME_ROLE = "veotrex_api"
SCHEMA = "public"

# Every attribute is asserted on each run so a role that drifted (for example an operator
# granting SUPERUSER "to make an error go away") is pulled back on the next apply.
ROLE_ATTRIBUTES = sql.SQL(
    "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT"
)

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
# Tables created by future migrations receive these, and only these, automatically. DELETE stays
# an explicit per-table decision below. Function EXECUTE is never granted by default.
DEFAULT_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE")


@dataclass(frozen=True, slots=True)
class TableGrant:
    table: str
    privileges: frozenset[str]

    def __post_init__(self) -> None:
        unknown = self.privileges - set(TABLE_PRIVILEGES)
        if unknown or not self.privileges:
            raise ValueError(f"invalid privilege set for {self.table}: {sorted(unknown)}")


def _grant(table: str, *privileges: str) -> TableGrant:
    return TableGrant(table, frozenset(privileges))


# The exact privilege inventory of the running API, derived from the statements the service code
# issues (see tests/test_runtime_role.py, which pins this to the ORM metadata). Anything not
# listed here or in RUNTIME_TABLES_WITHOUT_ACCESS is a classification error caught in tests.
RUNTIME_TABLE_GRANTS: tuple[TableGrant, ...] = (
    # Tenant name for the Ring link preview. No RLS on this table; read-only by design.
    _grant("tenants", "SELECT"),
    # Principal resolution and last-authentication bookkeeping (access.py).
    _grant("actors", "SELECT"),
    _grant("actor_identities", "SELECT", "UPDATE"),
    _grant("role_assignments", "SELECT"),
    # Ring linking, inventory reconciliation and webhook application.
    _grant("camera_provider_connections", "SELECT", "INSERT", "UPDATE"),
    _grant("cameras", "SELECT", "INSERT", "UPDATE"),
    _grant("camera_provider_devices", "SELECT", "INSERT", "UPDATE"),
    _grant("camera_provider_components", "SELECT", "INSERT", "UPDATE"),
    _grant("provider_events", "SELECT", "INSERT"),
    # Append-only from the runtime: no UPDATE or DELETE, so audit rows cannot be rewritten.
    _grant("audit_events", "SELECT", "INSERT"),
    # AEAD ciphertext store; rows are deleted on disconnect/removal (encrypted_vault.py).
    _grant("encrypted_credentials", "SELECT", "INSERT", "UPDATE", "DELETE"),
)

# Reached only through SECURITY DEFINER functions, or never by the API at all.
RUNTIME_TABLES_WITHOUT_ACCESS: frozenset[str] = frozenset(
    {
        "alembic_version",
        "ring_pending_links",
        "ring_webhook_inbox",
        "tenant_identity_bindings",
        "jurisdiction_policies",
        "policy_versions",
        "facilities",
        "areas",
        "zones",
        "edge_nodes",
        "camera_assignments",
    }
)

# The intentionally exposed SECURITY DEFINER surface. Each was created with a fixed
# ``search_path`` and ``REVOKE ALL ... FROM PUBLIC`` in its migration; EXECUTE is granted here
# to the runtime role alone. Signatures use the identity-argument form PostgreSQL reports.
RUNTIME_FUNCTION_GRANTS: tuple[str, ...] = (
    "resolve_tenant_identity_binding(text, text, text)",
    "create_ring_pending_link(uuid, text, integer, timestamp with time zone)",
    "complete_ring_pending_account(uuid, text)",
    "record_ring_pending_failure(uuid, text)",
    "list_ring_pending_candidates(timestamp with time zone)",
    "start_ring_pending_claim(uuid, uuid, uuid)",
    "transition_ring_pending_link(uuid, text, text, text)",
    "ingest_ring_webhook(uuid, text, text, timestamp with time zone, text, text, text, "
    "text, text, bigint, text, json, json)",
    "claim_next_ring_webhook()",
    "finish_ring_webhook(uuid, text, text, timestamp with time zone)",
    "resolve_ring_webhook_connection(text)",
)


class RuntimeRoleError(RuntimeError):
    """Provisioning or verification could not complete. Messages never carry secrets."""


def _scalar_int(row: tuple[object, ...] | None, *, default: int = 0) -> int:
    """First column of a catalog row as an int, tolerating psycopg's ``object`` typing."""
    if row is None:
        return default
    value = row[0]
    return value if isinstance(value, int) else default


# ------------------------------------------------------------------------------- credentials


def scram_sha256_verifier(
    password: str, *, iterations: int = 4096, salt: bytes | None = None
) -> str:
    """Return the ``SCRAM-SHA-256$<i>:<salt>$<StoredKey>:<ServerKey>`` verifier for a password.

    PostgreSQL accepts this pre-hashed form in ``ALTER ROLE ... PASSWORD``, so the plaintext never
    reaches the server, its statement log, or ``pg_stat_statements``. Only printable ASCII is
    accepted: it makes SASLprep the identity transform, and every generated credential satisfies it.
    """
    if not password or not password.isascii() or not password.isprintable():
        raise RuntimeRoleError("runtime password must be non-empty printable ASCII")
    if iterations < 4096:
        raise RuntimeRoleError("SCRAM iteration count below the PostgreSQL default is refused")
    raw_salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), raw_salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(raw_salt)}${b64(stored_key)}:{b64(server_key)}"


def generate_password() -> str:
    """A fresh 256-bit URL-safe credential; printable ASCII so the SCRAM path applies."""
    return secrets.token_urlsafe(32)


def psycopg_dsn(url: str) -> str:
    """Translate the SQLAlchemy ``postgresql+psycopg://`` form into a libpq URL."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgresql://"):
        return url
    raise RuntimeRoleError("database URL must use a postgresql scheme")


def replace_credentials(url: str, username: str, password: str) -> str:
    """Return ``url`` with its user-info replaced; used to derive a runtime DSN for tests."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{username}:{password}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ------------------------------------------------------------------------------------- plan


@dataclass(frozen=True, slots=True)
class PlanStep:
    description: str
    statement: sql.Composed
    sensitive: bool = False

    def render(self) -> str:
        if self.sensitive:
            return f"-- {self.description} (statement withheld: contains a credential verifier)"
        return f"-- {self.description}\n{self.statement.as_string()};"


@dataclass(frozen=True, slots=True)
class SchemaInventory:
    """Non-secret facts about the target database that shape the plan."""

    database: str
    connected_role: str
    role_exists: bool
    tables: frozenset[str]
    functions: frozenset[str]
    sequences: frozenset[str]


@dataclass(frozen=True, slots=True)
class RuntimeRolePlan:
    role: str
    migration_role: str
    database: str
    steps: tuple[PlanStep, ...] = field(default_factory=tuple)

    def render(self) -> str:
        header = (
            f"-- runtime role plan: role={self.role} migration_role={self.migration_role} "
            f"database={self.database}\n"
        )
        return header + "\n".join(step.render() for step in self.steps) + "\n"


def _ident(name: str) -> sql.Identifier:
    return sql.Identifier(name)


def _table(name: str) -> sql.Identifier:
    return sql.Identifier(SCHEMA, name)


def _function(signature: str) -> sql.Composed:
    # Signatures are constants defined in this module, never caller input, and the function
    # must exist in the inventory before it is referenced.
    name, _, arguments = signature.partition("(")
    return sql.SQL("{}.{}({}").format(_ident(SCHEMA), _ident(name), sql.SQL(arguments))


def inspect_schema(
    connection: psycopg.Connection[tuple[object, ...]], role: str
) -> SchemaInventory:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        database, connected_role = cursor.fetchone() or ("", "")
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        role_exists = bool((cursor.fetchone() or (False,))[0])
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p')",
            (SCHEMA,),
        )
        tables = frozenset(str(row[0]) for row in cursor.fetchall())
        cursor.execute(
            "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
            (SCHEMA,),
        )
        functions = frozenset(str(row[0]) for row in cursor.fetchall())
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'S'",
            (SCHEMA,),
        )
        sequences = frozenset(str(row[0]) for row in cursor.fetchall())
    return SchemaInventory(
        str(database), str(connected_role), role_exists, tables, functions, sequences
    )


def _normalise_signature(signature: str) -> str:
    """Compare signatures ignoring argument names, the way pg reports identity arguments."""
    name, _, arguments = signature.partition("(")
    types = []
    for argument in arguments.rstrip(")").split(","):
        token = argument.strip()
        if not token:
            continue
        parts = token.split(" ")
        # ``pending_id uuid`` -> ``uuid``; ``timestamp with time zone`` keeps its words.
        if len(parts) > 1 and parts[0] not in {"timestamp", "double", "character", "bit"}:
            token = " ".join(parts[1:])
        types.append(token)
    return f"{name}({', '.join(types)})"


def build_plan(
    inventory: SchemaInventory,
    *,
    role: str = DEFAULT_RUNTIME_ROLE,
    migration_role: str | None = None,
    password_verifier: str | None = None,
    require_credential: bool = True,
) -> RuntimeRolePlan:
    """Compose the idempotent statement sequence. Raises before any statement runs if the
    schema is missing a table or function the runtime needs (the database is not migrated).

    ``require_credential=False`` lets a dry run render the plan for a role that does not exist
    yet; ``apply`` always requires a credential to create one, since a LOGIN role without a
    password can never authenticate and would only look provisioned."""
    if not role or not role.isidentifier():
        raise RuntimeRoleError("runtime role name must be a plain identifier")
    owner = migration_role or inventory.connected_role
    if owner == role:
        raise RuntimeRoleError("the runtime role cannot also be the migration role")
    if not inventory.role_exists and password_verifier is None and require_credential:
        raise RuntimeRoleError(
            f"role {role!r} does not exist; a password reference is required to create it"
        )

    missing_tables = sorted(
        grant.table for grant in RUNTIME_TABLE_GRANTS if grant.table not in inventory.tables
    )
    if missing_tables:
        raise RuntimeRoleError(
            "database is not migrated: missing tables " + ", ".join(missing_tables)
        )
    present_functions = {_normalise_signature(item): item for item in inventory.functions}
    required = {_normalise_signature(item): item for item in RUNTIME_FUNCTION_GRANTS}
    missing_functions = sorted(required[key] for key in required if key not in present_functions)
    if missing_functions:
        raise RuntimeRoleError(
            "database is not migrated: missing functions " + ", ".join(missing_functions)
        )

    steps: list[PlanStep] = []
    if inventory.role_exists:
        steps.append(
            PlanStep(
                "re-assert restricted attributes on the existing role",
                sql.SQL("ALTER ROLE {} WITH {}").format(_ident(role), ROLE_ATTRIBUTES),
            )
        )
    else:
        steps.append(
            PlanStep(
                "create the restricted runtime role",
                sql.SQL("CREATE ROLE {} WITH {}").format(_ident(role), ROLE_ATTRIBUTES),
            )
        )
    if password_verifier is not None:
        steps.append(
            PlanStep(
                "set the runtime credential (SCRAM verifier; explicit request only)",
                sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    _ident(role), sql.Literal(password_verifier)
                ),
                sensitive=True,
            )
        )
    elif not inventory.role_exists:
        steps.append(
            PlanStep(
                "set the runtime credential: apply will require --password-ref",
                sql.SQL("-- ALTER ROLE {} WITH PASSWORD <verifier>").format(_ident(role)),
            )
        )

    database = _ident(inventory.database)
    steps.append(
        PlanStep(
            "database: CONNECT only",
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(database, _ident(role)),
        )
    )
    steps.append(
        PlanStep(
            "database: CONNECT only",
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, _ident(role)),
        )
    )
    steps.append(
        PlanStep(
            "schema: USAGE only, never CREATE",
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(_ident(SCHEMA), _ident(role)),
        )
    )
    steps.append(
        PlanStep(
            "schema: USAGE only, never CREATE",
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(_ident(SCHEMA), _ident(role)),
        )
    )

    for grant in RUNTIME_TABLE_GRANTS:
        steps.append(
            PlanStep(
                f"table {grant.table}: converge to {', '.join(sorted(grant.privileges))}",
                sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(_table(grant.table), _ident(role)),
            )
        )
        steps.append(
            PlanStep(
                f"table {grant.table}: converge to {', '.join(sorted(grant.privileges))}",
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(p) for p in sorted(grant.privileges)),
                    _table(grant.table),
                    _ident(role),
                ),
            )
        )
    for table in sorted(RUNTIME_TABLES_WITHOUT_ACCESS & inventory.tables):
        steps.append(
            PlanStep(
                f"table {table}: no runtime access",
                sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(_table(table), _ident(role)),
            )
        )
    for sequence in sorted(inventory.sequences):
        steps.append(
            PlanStep(
                f"sequence {sequence}: no runtime access",
                sql.SQL("REVOKE ALL ON SEQUENCE {} FROM {}").format(_table(sequence), _ident(role)),
            )
        )

    granted_keys = set(required)
    for key, signature in sorted(present_functions.items()):
        if key in granted_keys:
            steps.append(
                PlanStep(
                    f"function {signature}: EXECUTE",
                    sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                        _function(signature), _ident(role)
                    ),
                )
            )
        else:
            steps.append(
                PlanStep(
                    f"function {signature}: no runtime access",
                    sql.SQL("REVOKE ALL ON FUNCTION {} FROM {}").format(
                        _function(signature), _ident(role)
                    ),
                )
            )

    steps.append(
        PlanStep(
            f"default privileges for tables created by {owner}",
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT {} ON TABLES TO {}"
            ).format(
                _ident(owner),
                _ident(SCHEMA),
                sql.SQL(", ").join(sql.SQL(p) for p in DEFAULT_TABLE_PRIVILEGES),
                _ident(role),
            ),
        )
    )
    # Per-schema default privileges are ADDED to the global defaults and can never remove
    # PUBLIC's built-in EXECUTE on functions; only a global (schema-less) default for the
    # creating role does that. It applies to every schema that role creates functions in,
    # which is exactly the intent: new functions are executable by their owner only until a
    # later apply lists them in RUNTIME_FUNCTION_GRANTS. Pinned by tests/test_runtime_role.py.
    steps.append(
        PlanStep(
            f"future functions created by {owner} are not PUBLIC-executable",
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
            ).format(_ident(owner)),
        )
    )
    return RuntimeRolePlan(role, owner, inventory.database, tuple(steps))


def apply_plan(connection: psycopg.Connection[tuple[object, ...]], plan: RuntimeRolePlan) -> None:
    """Execute every step in one transaction so a failure leaves the cluster unchanged."""
    with connection.transaction(), connection.cursor() as cursor:
        for step in plan.steps:
            cursor.execute(step.statement)


# ------------------------------------------------------------------------------ verification


@dataclass(frozen=True, slots=True)
class RoleAttributes:
    name: str
    superuser: bool
    bypass_rls: bool
    create_role: bool
    create_db: bool
    replication: bool
    inherit: bool
    can_login: bool

    @property
    def restricted(self) -> bool:
        return not (
            self.superuser
            or self.bypass_rls
            or self.create_role
            or self.create_db
            or self.replication
            or self.inherit
        )


def role_attributes(
    connection: psycopg.Connection[tuple[object, ...]], role: str
) -> RoleAttributes:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
            "rolreplication, rolinherit, rolcanlogin FROM pg_roles WHERE rolname = %s",
            (role,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeRoleError(f"role {role!r} does not exist")
    return RoleAttributes(*(str(row[0]), *(bool(value) for value in row[1:])))  # type: ignore[arg-type]


def verify(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    role: str = DEFAULT_RUNTIME_ROLE,
    migration_role: str | None = None,
) -> list[str]:
    """Return the list of discrepancies between the intended and effective privilege model.

    Runs as the admin identity and reads catalog metadata only. An empty list means verified.
    """
    problems: list[str] = []
    attributes = role_attributes(connection, role)
    if not attributes.can_login:
        problems.append("role cannot LOGIN")
    for flag, value in (
        ("SUPERUSER", attributes.superuser),
        ("BYPASSRLS", attributes.bypass_rls),
        ("CREATEROLE", attributes.create_role),
        ("CREATEDB", attributes.create_db),
        ("REPLICATION", attributes.replication),
        ("INHERIT", attributes.inherit),
    ):
        if value:
            problems.append(f"role has {flag}")

    inventory = inspect_schema(connection, role)
    owner = migration_role or inventory.connected_role
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )
        if (cursor.fetchone() or ("",))[0] == role:
            problems.append("role owns the database")
        cursor.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (SCHEMA,)
        )
        if (cursor.fetchone() or ("",))[0] == role:
            problems.append("role owns the application schema")
        cursor.execute("SELECT has_schema_privilege(%s, %s, 'CREATE')", (role, SCHEMA))
        if bool((cursor.fetchone() or (False,))[0]):
            problems.append("role can CREATE in the application schema")
        cursor.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, SCHEMA))
        if not bool((cursor.fetchone() or (False,))[0]):
            problems.append("role lacks USAGE on the application schema")
        cursor.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','S','v') "
            "AND pg_get_userbyid(c.relowner) = %s",
            (SCHEMA, role),
        )
        if _scalar_int(cursor.fetchone()) > 0:
            problems.append("role owns application relations")

        expected = {grant.table: grant.privileges for grant in RUNTIME_TABLE_GRANTS}
        for table in sorted(inventory.tables):
            wanted = expected.get(table, frozenset())
            if table not in expected and table not in RUNTIME_TABLES_WITHOUT_ACCESS:
                continue  # unknown to this build: default privileges apply, nothing to assert
            for privilege in TABLE_PRIVILEGES:
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"{SCHEMA}.{table}", privilege),
                )
                actual = bool((cursor.fetchone() or (False,))[0])
                if actual != (privilege in wanted):
                    problems.append(
                        f"table {table}: {privilege} is {'granted' if actual else 'missing'}"
                    )
        for sequence in sorted(inventory.sequences):
            cursor.execute(
                "SELECT has_sequence_privilege(%s, %s, 'USAGE') "
                "OR has_sequence_privilege(%s, %s, 'SELECT') "
                "OR has_sequence_privilege(%s, %s, 'UPDATE')",
                (role, f"{SCHEMA}.{sequence}") * 3,
            )
            if bool((cursor.fetchone() or (False,))[0]):
                problems.append(f"sequence {sequence}: runtime privilege granted")

        wanted_functions = {_normalise_signature(item) for item in RUNTIME_FUNCTION_GRANTS}
        for signature in sorted(inventory.functions):
            # has_function_privilege takes a regprocedure: types only, no argument names.
            cursor.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (role, f"{SCHEMA}.{_normalise_signature(signature)}"),
            )
            actual = bool((cursor.fetchone() or (False,))[0])
            if actual != (_normalise_signature(signature) in wanted_functions):
                problems.append(
                    f"function {signature}: EXECUTE is {'granted' if actual else 'missing'}"
                )

        cursor.execute(
            "SELECT d.defaclobjtype, d.defaclacl::text FROM pg_default_acl d "
            "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
            "WHERE pg_get_userbyid(d.defaclrole) = %s AND n.nspname = %s",
            (owner, SCHEMA),
        )
        defaults = {str(kind): str(acl) for kind, acl in cursor.fetchall()}
        table_default = defaults.get("r", "")
        if f"{role}=" not in table_default and f'"{role}"=' not in table_default:
            problems.append(f"no default table privileges for tables created by {owner}")
        # The function default is global (defaclnamespace = 0); see build_plan.
        cursor.execute(
            "SELECT d.defaclacl::text FROM pg_default_acl d "
            "WHERE pg_get_userbyid(d.defaclrole) = %s AND d.defaclnamespace = 0 "
            "AND d.defaclobjtype = 'f'",
            (owner,),
        )
        function_default = cursor.fetchone()
        acl = "" if function_default is None else str(function_default[0])
        # An aclitem whose grantee is PUBLIC renders with an empty name before "=".
        if function_default is None or "{=" in acl or ",=" in acl:
            problems.append(f"functions created by {owner} still default to PUBLIC EXECUTE")
    return problems


# --------------------------------------------------------------------------------- probing


@dataclass(frozen=True, slots=True)
class ProbeResult:
    check: str
    passed: bool
    detail: str


def probe(
    connection: psycopg.Connection[tuple[object, ...]], *, migration_role: str | None = None
) -> list[ProbeResult]:
    """Isolation probes executed AS the runtime role over its own connection.

    Each negative probe runs in its own transaction so an expected failure never poisons the
    next check. Nothing here prints a row of tenant data; only counts and outcomes.
    """
    results: list[ProbeResult] = []
    connection.rollback()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
            "rolreplication FROM pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone() or ("", True, True, True, True, True)
    connection.rollback()
    flags = [bool(value) for value in row[1:]]
    results.append(
        ProbeResult(
            "runtime role attributes",
            not any(flags),
            f"current_user={row[0]} superuser={flags[0]} bypassrls={flags[1]} "
            f"createrole={flags[2]} createdb={flags[3]} replication={flags[4]}",
        )
    )

    INSUFFICIENT_PRIVILEGE = "42501"

    def expect_refused(check: str, *statements: str, autocommit: bool = False) -> None:
        """PASS only when PostgreSQL refuses with insufficient_privilege (SQLSTATE 42501).

        Any other error is reported as a failure too: a probe that "fails" for an unrelated
        reason (a syntax error, a transaction-block restriction) proves nothing about privilege.
        """
        try:
            if autocommit:
                # CREATE DATABASE cannot run inside a transaction block; without autocommit the
                # refusal would be 25001, which says nothing about the role's privileges.
                connection.autocommit = True
                try:
                    with connection.cursor() as cursor:
                        for statement in statements:
                            cursor.execute(statement)
                finally:
                    connection.autocommit = False
            else:
                with connection.transaction(), connection.cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)
        except psycopg.Error as exc:
            code = exc.sqlstate or "unknown"
            results.append(ProbeResult(check, code == INSUFFICIENT_PRIVILEGE, f"refused ({code})"))
            return
        results.append(ProbeResult(check, False, "statement succeeded"))

    def expect_count(check: str, statement: str, expected: int) -> None:
        try:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(statement)
                value = _scalar_int(cursor.fetchone(), default=-1)
        except psycopg.Error as exc:
            results.append(ProbeResult(check, False, f"query failed ({exc.sqlstate})"))
            return
        results.append(ProbeResult(check, value == expected, f"rows={value}"))

    expect_count(
        "tenant table without tenant context discloses nothing",
        "SELECT count(*) FROM public.camera_provider_connections",
        0,
    )
    expect_count(
        "tenant table without tenant context discloses nothing (cameras)",
        "SELECT count(*) FROM public.cameras",
        0,
    )
    expect_refused(
        "cannot read the webhook inbox directly", "SELECT count(*) FROM public.ring_webhook_inbox"
    )
    expect_refused(
        "cannot read pending links directly", "SELECT count(*) FROM public.ring_pending_links"
    )
    expect_refused(
        "cannot read identity bindings directly",
        "SELECT count(*) FROM public.tenant_identity_bindings",
    )
    expect_refused("cannot create tables", "CREATE TABLE public.zz_runtime_probe (id int)")
    expect_refused("cannot create roles", "CREATE ROLE zz_runtime_probe NOLOGIN")
    expect_refused("cannot create databases", "CREATE DATABASE zz_runtime_probe", autocommit=True)
    expect_refused(
        "cannot disable row level security",
        "ALTER TABLE public.cameras DISABLE ROW LEVEL SECURITY",
    )
    expect_refused(
        "cannot alter the isolation policy",
        "ALTER POLICY tenant_isolation ON public.cameras USING (true)",
    )
    expect_refused(
        "cannot take ownership of application tables",
        "ALTER TABLE public.cameras OWNER TO CURRENT_USER",
    )
    # Any role may SET row_security = off; what matters is that PostgreSQL then refuses to
    # run a query the policy would have filtered, instead of bypassing it.
    expect_refused(
        "row_security = off cannot bypass the policy",
        "SET LOCAL row_security = off",
        "SELECT count(*) FROM public.cameras",
    )
    expect_refused("cannot delete audit rows", "DELETE FROM public.audit_events")
    if migration_role:
        expect_refused(
            f"cannot SET ROLE to {migration_role}",
            sql.SQL("SET ROLE {}").format(_ident(migration_role)).as_string(),
        )
    return results


# ------------------------------------------------------------------------------ test support


def provision_for_tests(admin_url: str, *, role: str) -> str | None:
    """Create or refresh a runtime role for the automated suite and return its DSN.

    Uses the admin (cluster) identity from ``admin_url`` for provisioning only. A fresh password
    is generated per session and exists solely in the returned URL. Returns ``None`` when the
    admin target cannot be reached, so callers fall back to the unreachable sentinel.
    """
    password = generate_password()
    try:
        with psycopg.connect(psycopg_dsn(admin_url), autocommit=False) as connection:
            inventory = inspect_schema(connection, role)
            plan = build_plan(
                inventory, role=role, password_verifier=scram_sha256_verifier(password)
            )
            apply_plan(connection, plan)
    except psycopg.OperationalError:
        return None
    return replace_credentials(admin_url, role, password)


# ---------------------------------------------------------------------------------- console


def _resolve_url(reference: str | None, fallback_secret: str | None) -> str:
    if reference:
        from veotrex_api.secrets import DefaultSecretResolver, SecretResolutionError

        try:
            return DefaultSecretResolver().resolve(reference).get_secret_value()
        except SecretResolutionError as exc:
            raise RuntimeRoleError(f"database URL reference is unusable: {exc}") from None
    if fallback_secret is None:
        raise RuntimeRoleError("no database URL configured")
    return fallback_secret


def _settings_database_url() -> str | None:
    try:
        from veotrex_api.config import get_settings

        return get_settings().database_url.get_secret_value()
    except ValueError:
        # pydantic's ValidationError is a ValueError: unset settings are a normal CLI condition
        # when --url-ref is supplied instead.
        return None


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-db-runtime-role",
        description=(
            "Provision, verify or probe the restricted PostgreSQL role the VeoTrex API runs as. "
            "plan/apply/verify connect with the ADMIN identity; probe connects AS the runtime role."
        ),
    )
    command.add_argument("action", choices=("plan", "apply", "verify", "probe"))
    command.add_argument("--role", default=DEFAULT_RUNTIME_ROLE)
    command.add_argument(
        "--migration-role",
        default=None,
        help="role that executes Alembic (default: the admin connection's current_user)",
    )
    command.add_argument(
        "--url-ref",
        default=None,
        help=(
            "secret reference (env:NAME or file:/abs) to the DSN to connect with; defaults to "
            "the configured VEOTREX_DATABASE_URL / VEOTREX_DATABASE_URL_REF"
        ),
    )
    command.add_argument(
        "--password-ref",
        default=None,
        help="apply only: secret reference to the runtime password to set (never a value)",
    )
    return command


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        url = _resolve_url(arguments.url_ref, _settings_database_url())
        if arguments.action == "probe":
            with psycopg.connect(psycopg_dsn(url)) as connection:
                results = probe(connection, migration_role=arguments.migration_role)
            failed = False
            for result in results:
                print(f"{'PASS' if result.passed else 'FAIL'}  {result.check}: {result.detail}")
                failed = failed or not result.passed
            return 1 if failed else 0

        verifier: str | None = None
        if arguments.password_ref:
            if arguments.action != "apply":
                raise RuntimeRoleError("--password-ref is only meaningful with apply")
            from veotrex_api.secrets import DefaultSecretResolver, SecretResolutionError

            try:
                secret = DefaultSecretResolver().resolve(arguments.password_ref)
            except SecretResolutionError as exc:
                raise RuntimeRoleError(f"password reference is unusable: {exc}") from None
            verifier = scram_sha256_verifier(secret.get_secret_value())

        with psycopg.connect(psycopg_dsn(url), autocommit=False) as connection:
            if arguments.action == "verify":
                problems = verify(
                    connection, role=arguments.role, migration_role=arguments.migration_role
                )
                attributes = role_attributes(connection, arguments.role)
                print(
                    f"role={attributes.name} superuser={attributes.superuser} "
                    f"bypassrls={attributes.bypass_rls} createrole={attributes.create_role} "
                    f"createdb={attributes.create_db} replication={attributes.replication} "
                    f"inherit={attributes.inherit} login={attributes.can_login}"
                )
                for problem in problems:
                    print(f"PROBLEM  {problem}")
                print("verified" if not problems else f"{len(problems)} problem(s)")
                return 0 if not problems else 1

            inventory = inspect_schema(connection, arguments.role)
            plan = build_plan(
                inventory,
                role=arguments.role,
                migration_role=arguments.migration_role,
                password_verifier=verifier,
                require_credential=arguments.action == "apply",
            )
            if arguments.action == "plan":
                print(plan.render(), end="")
                return 0
            apply_plan(connection, plan)
            problems = verify(connection, role=arguments.role, migration_role=plan.migration_role)
            print(f"applied {len(plan.steps)} statements for role {plan.role}")
            for problem in problems:
                print(f"PROBLEM  {problem}")
            return 0 if not problems else 1
    except (RuntimeRoleError, psycopg.Error) as exc:
        # psycopg messages name objects and states, never the credential.
        print(f"runtime role {arguments.action} failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - console wrapper
    raise SystemExit(main())
