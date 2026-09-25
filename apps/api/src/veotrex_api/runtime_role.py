"""PostgreSQL runtime-role provisioning for the VeoTrex API (V1-00A, corrected in V1-00A-R1).

Row Level Security is only a tenant boundary when the connecting role is subject to it. A
superuser, or any role with ``BYPASSRLS``, reads every tenant's rows regardless of ``FORCE ROW
LEVEL SECURITY``. This module provisions the restricted role the API must connect as and applies
exactly the privilege set the current code needs, and nothing else.

Three identities are involved and deliberately kept apart:

``bootstrap / admin``
    The cluster's bootstrap superuser (``POSTGRES_USER``). Runs this module. Never the API.
``migration``
    The role that owns the schema and executes Alembic. Today it is the same role as the
    bootstrap identity.
``runtime``
    ``LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT``, owns
    nothing, cannot ``CREATE`` in the schema, belongs to no other role, and holds only the table
    and function privileges enumerated in :data:`TABLE_CLASSIFICATION` and
    :data:`RUNTIME_FUNCTION_GRANTS`. The API connects as this role.

**The model fails closed.** A table, function or sequence that a migration introduces is
inaccessible to the runtime role until a developer classifies it here and ``apply`` is re-run.
No ``ALTER DEFAULT PRIVILEGES`` grants the runtime anything; the only defaults installed remove
privileges PostgreSQL would otherwise hand to PUBLIC (function EXECUTE). ``apply`` converges an
existing database to this model: it revokes what should not exist as well as granting what
should, including on tables it does not classify and on default-privilege entries an earlier
version installed. A unit test pins the classification to the ORM metadata so an unclassified
table fails CI.

Role provisioning is cluster-level administration, not an application schema change, so it lives
here behind a privileged console script rather than in an Alembic migration. Running it twice is
a no-op; the password is touched only when a password reference is supplied explicitly, and it
is sent as a pre-computed SCRAM-SHA-256 verifier so the plaintext never appears in server logs.

No password, DSN or secret value is ever printed, logged or included in an exception.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
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
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


class TableAccess(StrEnum):
    """How the API runtime role may touch a table. Every application table declares one."""

    RUNTIME_READ = "RUNTIME_READ"  # SELECT only
    RUNTIME_APPEND_ONLY = "RUNTIME_APPEND_ONLY"  # SELECT, INSERT; rows can never be rewritten
    RUNTIME_WRITE = "RUNTIME_WRITE"  # explicit privilege set, DELETE never inferred
    FUNCTION_ONLY = "FUNCTION_ONLY"  # no table privilege; reached via SECURITY DEFINER only
    RUNTIME_NO_ACCESS = "RUNTIME_NO_ACCESS"  # the API never touches it


@dataclass(frozen=True, slots=True)
class TableClassification:
    access: TableAccess
    privileges: frozenset[str] = frozenset()
    reason: str = ""

    def __post_init__(self) -> None:
        unknown = self.privileges - set(TABLE_PRIVILEGES)
        if unknown:
            raise ValueError(f"unknown privileges {sorted(unknown)}")
        expected: frozenset[str] | None
        if self.access is TableAccess.RUNTIME_READ:
            expected = frozenset({"SELECT"})
        elif self.access is TableAccess.RUNTIME_APPEND_ONLY:
            expected = frozenset({"SELECT", "INSERT"})
        elif self.access is TableAccess.RUNTIME_WRITE:
            expected = None
            if not self.privileges:
                raise ValueError("RUNTIME_WRITE requires an explicit privilege set")
        else:
            expected = frozenset()
        if expected is not None and self.privileges != expected:
            raise ValueError(f"{self.access} implies exactly {sorted(expected)}")


def _read(reason: str) -> TableClassification:
    return TableClassification(TableAccess.RUNTIME_READ, frozenset({"SELECT"}), reason)


def _append(reason: str) -> TableClassification:
    return TableClassification(
        TableAccess.RUNTIME_APPEND_ONLY, frozenset({"SELECT", "INSERT"}), reason
    )


def _write(reason: str, *privileges: str) -> TableClassification:
    return TableClassification(TableAccess.RUNTIME_WRITE, frozenset(privileges), reason)


def _function_only(reason: str) -> TableClassification:
    return TableClassification(TableAccess.FUNCTION_ONLY, frozenset(), reason)


def _no_access(reason: str) -> TableClassification:
    return TableClassification(TableAccess.RUNTIME_NO_ACCESS, frozenset(), reason)


# The canonical classification of every application table. tests/test_runtime_role.py fails
# when an ORM table is missing here, so a new table cannot reach production unclassified.
TABLE_CLASSIFICATION: Mapping[str, TableClassification] = {
    # RLS self-policy (migration 0006): a request reads only its own tenant row.
    "tenants": _read("tenant name for the Ring link preview and claim"),
    "actors": _read("principal resolution"),
    "actor_identities": _write(
        "principal resolution and last-authentication mark", "SELECT", "UPDATE"
    ),
    "role_assignments": _read("permission resolution"),
    "camera_provider_connections": _write(
        "Ring linking, refresh bookkeeping, inventory, disconnect", "SELECT", "INSERT", "UPDATE"
    ),
    "cameras": _write("inventory reconciliation", "SELECT", "INSERT", "UPDATE"),
    "camera_provider_devices": _write("inventory reconciliation", "SELECT", "INSERT", "UPDATE"),
    "camera_provider_components": _write("inventory reconciliation", "SELECT", "INSERT", "UPDATE"),
    # Staff enrollment (V1-02A): soft delete only, DELETE never granted.
    "staff_profiles": _write("staff roster and enrollment state", "SELECT", "INSERT", "UPDATE"),
    "staff_enrollment_images": _write(
        "enrollment photo metadata; bytes live in the private media store",
        "SELECT",
        "INSERT",
        "UPDATE",
    ),
    "staff_face_templates": _write(
        "biometric templates; revoked by status, exported only by the admin package tool",
        "SELECT",
        "INSERT",
        "UPDATE",
    ),
    "provider_events": _append("provider telemetry, deduplicated on request id"),
    "audit_events": _append("audit trail; the runtime can never rewrite or remove an entry"),
    # Global by design; reached only through the vault functions (migration 0006).
    "encrypted_credentials": _function_only("vault_credential_* SECURITY DEFINER functions"),
    "ring_pending_links": _function_only("pending-link state machine functions"),
    "ring_webhook_inbox": _function_only("webhook inbox functions"),
    "tenant_identity_bindings": _function_only("resolve_tenant_identity_binding"),
    # Machine credentials (migration 0008): digests only, reached through the authentication
    # function alone, so a compromised API cannot enumerate or rewrite them.
    "edge_node_credentials": _function_only("authenticate_edge_node_credential"),
    "alembic_version": _no_access("migration bookkeeping"),
    "jurisdiction_policies": _no_access("policy catalog is file-backed in the API today"),
    "policy_versions": _no_access("policy catalog is file-backed in the API today"),
    "facilities": _no_access("no API endpoint yet"),
    "areas": _no_access("no API endpoint yet"),
    "zones": _no_access("no API endpoint yet"),
    # Edge WHEP broker (V1-DEMO-03B): read-only camera authorization of an authenticated node.
    # Nodes and assignments are still managed only by the admin identity.
    "edge_nodes": _read("edge broker: authenticated node status in camera authorization"),
    "camera_assignments": _read("edge broker: the node's active camera assignment"),
}

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
    "vault_credential_create(uuid, text, text, uuid, integer, bytea, bytea)",
    "vault_credential_open(uuid, text, text, uuid)",
    "vault_credential_replace(uuid, text, text, uuid, integer, integer, bytea, bytea)",
    "vault_credential_delete(uuid, text, text, uuid)",
    "authenticate_edge_node_credential(uuid, bytea)",
)
# ``vault_credential_authorized`` is deliberately absent: it is the private predicate the four
# vault functions call as their owner, and the runtime must not be able to probe it.


class RuntimeRoleError(RuntimeError):
    """Provisioning or verification could not complete. Messages never carry secrets."""


def _scalar_int(row: tuple[object, ...] | None, *, default: int = 0) -> int:
    """First column of a catalog row as an int, tolerating psycopg's ``object`` typing."""
    if row is None:
        return default
    value = row[0]
    return value if isinstance(value, int) else default


def _scalar_bool(row: tuple[object, ...] | None) -> bool:
    return bool(row[0]) if row else False


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
    # Signatures are module constants or catalog output, never caller input, and the function
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
        role_exists = _scalar_bool(cursor.fetchone())
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
    classification: Mapping[str, TableClassification] = TABLE_CLASSIFICATION,
    function_grants: Sequence[str] = RUNTIME_FUNCTION_GRANTS,
) -> RuntimeRolePlan:
    """Compose the idempotent, converging statement sequence.

    Raises before any statement runs if the schema is missing a table or function the runtime
    needs (the database is not migrated). ``classification`` and ``function_grants`` exist so
    tests can prove a deliberately classified object becomes reachable; production always uses
    the module constants.

    ``require_credential=False`` lets a dry run render the plan for a role that does not exist
    yet; ``apply`` always requires a credential to create one, since a LOGIN role without a
    password can never authenticate and would only look provisioned.
    """
    if not role or not role.isidentifier():
        raise RuntimeRoleError("runtime role name must be a plain identifier")
    owner = migration_role or inventory.connected_role
    if owner == role:
        raise RuntimeRoleError("the runtime role cannot also be the migration role")
    if not inventory.role_exists and password_verifier is None and require_credential:
        raise RuntimeRoleError(
            f"role {role!r} does not exist; a password reference is required to create it"
        )

    granted_tables = {
        table: entry.privileges for table, entry in classification.items() if entry.privileges
    }
    missing_tables = sorted(table for table in granted_tables if table not in inventory.tables)
    if missing_tables:
        raise RuntimeRoleError(
            "database is not migrated: missing tables " + ", ".join(missing_tables)
        )
    present_functions = {_normalise_signature(item): item for item in inventory.functions}
    required = {_normalise_signature(item): item for item in function_grants}
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

    # Tables: every table present in the schema converges. Classified tables get exactly their
    # privilege set; FUNCTION_ONLY, RUNTIME_NO_ACCESS and *unclassified* tables are revoked, so a
    # table this build does not know about (a newer migration, a stray object) fails closed.
    for table in sorted(inventory.tables):
        privileges = granted_tables.get(table, frozenset())
        label = ", ".join(sorted(privileges)) if privileges else "no runtime access"
        if table not in classification:
            label = "UNCLASSIFIED: no runtime access until classified"
        steps.append(
            PlanStep(
                f"table {table}: {label}",
                sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(_table(table), _ident(role)),
            )
        )
        if privileges:
            steps.append(
                PlanStep(
                    f"table {table}: {label}",
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        sql.SQL(", ").join(sql.SQL(p) for p in sorted(privileges)),
                        _table(table),
                        _ident(role),
                    ),
                )
            )
    for sequence in sorted(inventory.sequences):
        steps.append(
            PlanStep(
                f"sequence {sequence}: no runtime access",
                sql.SQL("REVOKE ALL ON SEQUENCE {} FROM {}").format(_table(sequence), _ident(role)),
            )
        )

    for key, signature in sorted(present_functions.items()):
        if key in required:
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

    # Default privileges: NEVER a grant to the runtime role. Objects a migration creates are
    # inaccessible until classified above and apply is re-run. An earlier version installed a
    # per-schema table default for the runtime; revoking it here converges old deployments.
    for kind, label in (
        ("TABLES", "tables"),
        ("SEQUENCES", "sequences"),
        ("FUNCTIONS", "functions"),
    ):
        steps.append(
            PlanStep(
                f"no automatic runtime access to {label} created by {owner} (converge)",
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON {} FROM {}"
                ).format(_ident(owner), _ident(SCHEMA), sql.SQL(kind), _ident(role)),
            )
        )
    # Safe default denial: PostgreSQL grants PUBLIC EXECUTE on every new function. Per-schema
    # defaults are ADDED to the global defaults and can never remove that, so the revoke must be
    # global for the creating role (verified empirically on PostgreSQL 17 and pinned by tests).
    # Tables and sequences carry no PUBLIC privilege by default; the revokes state the intent
    # explicitly and are no-ops today.
    for kind, label in (
        ("FUNCTIONS", "functions"),
        ("SEQUENCES", "sequences"),
        ("TABLES", "tables"),
    ):
        steps.append(
            PlanStep(
                f"future {label} created by {owner} carry no PUBLIC privilege",
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL ON {} FROM PUBLIC").format(
                    _ident(owner), sql.SQL(kind)
                ),
            )
        )
    # Defense in depth for the runtime role's OWN defaults. It cannot create functions today
    # (no CREATE anywhere), but if it ever gains creation rights in some schema, PostgreSQL
    # would grant PUBLIC EXECUTE on what it creates. Global, like the migration role's revoke.
    steps.append(
        PlanStep(
            f"functions created by {role} itself would not be PUBLIC-executable",
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
            ).format(_ident(role)),
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
    classification: Mapping[str, TableClassification] = TABLE_CLASSIFICATION,
    function_grants: Sequence[str] = RUNTIME_FUNCTION_GRANTS,
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
    if migration_role is None and inventory.connected_role == role:
        # The migration role cannot be inferred from the runtime role's own connection, and
        # evaluating the migration-role invariants against the runtime role would be wrong in
        # both directions. Fail loudly rather than guess.
        raise RuntimeRoleError(
            "verify is connected as the runtime role; run it with the admin identity or pass "
            "--migration-role explicitly"
        )
    owner = migration_role or inventory.connected_role
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s)",
            (role,),
        )
        if _scalar_int(cursor.fetchone()) > 0:
            problems.append("role is a member of another role")
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
        if _scalar_bool(cursor.fetchone()):
            problems.append("role can CREATE in the application schema")
        cursor.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, SCHEMA))
        if not _scalar_bool(cursor.fetchone()):
            problems.append("role lacks USAGE on the application schema")
        cursor.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','S','v','m') "
            "AND pg_get_userbyid(c.relowner) = %s",
            (SCHEMA, role),
        )
        if _scalar_int(cursor.fetchone()) > 0:
            problems.append("role owns application relations")

        for table in sorted(inventory.tables):
            entry = classification.get(table)
            if entry is None:
                problems.append(f"table {table}: unclassified")
            wanted = entry.privileges if entry is not None else frozenset()
            for privilege in TABLE_PRIVILEGES:
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"{SCHEMA}.{table}", privilege),
                )
                actual = _scalar_bool(cursor.fetchone())
                if actual != (privilege in wanted):
                    problems.append(
                        f"table {table}: {privilege} is {'granted' if actual else 'missing'}"
                    )
        for sequence in sorted(inventory.sequences):
            for privilege in SEQUENCE_PRIVILEGES:
                cursor.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)",
                    (role, f"{SCHEMA}.{sequence}", privilege),
                )
                if _scalar_bool(cursor.fetchone()):
                    problems.append(f"sequence {sequence}: {privilege} granted")

        wanted_functions = {_normalise_signature(item) for item in function_grants}
        for signature in sorted(inventory.functions):
            # has_function_privilege takes a regprocedure: types only, no argument names.
            regprocedure = f"{SCHEMA}.{_normalise_signature(signature)}"
            cursor.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, regprocedure))
            actual = _scalar_bool(cursor.fetchone())
            if actual != (_normalise_signature(signature) in wanted_functions):
                problems.append(
                    f"function {signature}: EXECUTE is {'granted' if actual else 'missing'}"
                )
            cursor.execute(
                "SELECT has_function_privilege('public', %s, 'EXECUTE')", (regprocedure,)
            )
            if _scalar_bool(cursor.fetchone()):
                problems.append(f"function {signature}: PUBLIC can execute")

        # Default privileges: nothing may ever be granted to the runtime role by default, and
        # the global function default for the migration role must exclude PUBLIC.
        cursor.execute(
            "SELECT d.defaclobjtype, coalesce(n.nspname, ''), d.defaclacl::text "
            "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
            "WHERE pg_get_userbyid(d.defaclrole) = %s",
            (owner,),
        )
        function_default_seen = False
        for kind, namespace, acl in cursor.fetchall():
            acl_text = str(acl)
            if f"{role}=" in acl_text or f'"{role}"=' in acl_text:
                problems.append(
                    f"default privileges grant the runtime role access to new {kind!s} objects"
                )
            if str(kind) == "f" and namespace == "":
                function_default_seen = True
                if "{=" in acl_text or ",=" in acl_text:
                    problems.append(f"functions created by {owner} still default to PUBLIC")
        if not function_default_seen:
            problems.append(f"functions created by {owner} still default to PUBLIC EXECUTE")
        # The runtime role's own creator defaults, independent of the migration role's.
        cursor.execute(
            "SELECT d.defaclacl::text FROM pg_default_acl d "
            "WHERE pg_get_userbyid(d.defaclrole) = %s AND d.defaclnamespace = 0 "
            "AND d.defaclobjtype = 'f'",
            (role,),
        )
        own_default = cursor.fetchone()
        own_acl = "" if own_default is None else str(own_default[0])
        if own_default is None or "{=" in own_acl or ",=" in own_acl:
            problems.append(
                f"functions created by the runtime role {role} would default to PUBLIC EXECUTE"
            )
    return problems


# --------------------------------------------------------------------------------- probing


@dataclass(frozen=True, slots=True)
class ProbeResult:
    check: str
    passed: bool
    detail: str


PROBE_ARTIFACT = "zz_runtime_probe"
PROBE_ABORTED = "probe aborted before mutation-capable checks"
INSUFFICIENT_PRIVILEGE = "42501"

# The identity gate. Read-only catalog facts about the CONNECTED role, compared against the
# name the operator asked for. Everything the probe later tries (DDL, ALTER, DELETE, SET ROLE)
# is only ever attempted once this has passed, so a superuser, the migration role, or simply a
# role other than the one named can never reach a mutation-capable statement.
_IDENTITY_SQL = (
    "SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, "
    "r.rolreplication, r.rolinherit, has_schema_privilege(current_user, %s, 'CREATE'), "
    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    " WHERE n.nspname = %s AND c.relkind IN ('r','p','S','v','m') "
    " AND c.relowner = r.oid), "
    "(SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) "
    "FROM pg_roles r WHERE r.rolname = current_user"
)

_ARTIFACT_SQL = (
    "SELECT "
    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    " WHERE n.nspname = %s AND c.relname = %s), "
    "(SELECT count(*) FROM pg_roles WHERE rolname = %s), "
    "(SELECT count(*) FROM pg_database WHERE datname = %s)"
)


def probe_identity_gate(
    connection: psycopg.Connection[tuple[object, ...]], *, role: str
) -> ProbeResult:
    """Read-only: is the connected role exactly the restricted runtime identity ``role``?

    Any deviation fails the gate: a different role name, SUPERUSER, BYPASSRLS, CREATEROLE,
    CREATEDB, REPLICATION, INHERIT, CREATE on the application schema, owning any application
    relation, or membership in any role. "Restricted enough" is not accepted.
    """
    connection.rollback()
    try:
        with connection.cursor() as cursor:
            cursor.execute(_IDENTITY_SQL, (SCHEMA, SCHEMA))
            row = cursor.fetchone()
    finally:
        connection.rollback()
    if row is None:
        return ProbeResult("runtime role identity", False, "current_user not found in pg_roles")
    name = str(row[0])
    flags = [bool(value) for value in row[1:8]]
    owned = _scalar_int((row[8],))
    memberships = _scalar_int((row[9],))
    detail = (
        f"current_user={name} expected={role} superuser={flags[0]} bypassrls={flags[1]} "
        f"createrole={flags[2]} createdb={flags[3]} replication={flags[4]} inherit={flags[5]} "
        f"schema_create={flags[6]} owned_relations={owned} role_memberships={memberships}"
    )
    passed = name == role and not any(flags) and owned == 0 and memberships == 0
    return ProbeResult("runtime role identity", passed, detail)


def probe_artifacts(connection: psycopg.Connection[tuple[object, ...]]) -> ProbeResult:
    """Read-only: does a table, role or database named ``zz_runtime_probe`` already exist?

    Their presence means an earlier run persisted a negative test (the V1-01A-2 incident).
    The probe reports and stops; it never drops anything - it is a verifier, not a cleanup.
    """
    connection.rollback()
    try:
        with connection.cursor() as cursor:
            cursor.execute(_ARTIFACT_SQL, (SCHEMA, PROBE_ARTIFACT, PROBE_ARTIFACT, PROBE_ARTIFACT))
            row = cursor.fetchone() or (1, 1, 1)
    finally:
        connection.rollback()
    table, role, database = (_scalar_int((value,)) for value in row)
    return ProbeResult(
        "no prior probe artifacts",
        table == 0 and role == 0 and database == 0,
        f"table={table} role={role} database={database} (name {PROBE_ARTIFACT}; "
        "leftovers are never dropped by the probe)",
    )


def execute_rollback_only(
    connection: psycopg.Connection[tuple[object, ...]], *statements: str
) -> tuple[str, str | None]:
    """Run ``statements`` in one transaction that is ALWAYS rolled back.

    Returns ``("refused", sqlstate)`` when PostgreSQL raised, or ``("succeeded", None)``
    when every statement ran. The rollback is explicit and unconditional: a protection that
    is accidentally missing yields a FAIL result and an unchanged database, never a committed
    change. ``SET LOCAL``/``SET LOCAL ROLE`` effects end with the transaction too, and
    ``RESET ROLE`` is issued afterwards as belt and braces.
    """
    connection.rollback()
    outcome: tuple[str, str | None]
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        outcome = ("succeeded", None)
    except psycopg.Error as exc:
        outcome = ("refused", exc.sqlstate or "unknown")
    finally:
        connection.rollback()
    try:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    except psycopg.Error:
        pass
    finally:
        connection.rollback()
    return outcome


def probe(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    role: str = DEFAULT_RUNTIME_ROLE,
    migration_role: str | None = None,
) -> list[ProbeResult]:
    """Isolation probes executed AS the runtime role over its own connection - fail closed.

    Phase 1 is a read-only identity gate: the connected role must be exactly ``role`` with
    every restricted attribute. Phase 2 is a read-only artifact check. Only when both pass do
    the active checks run, and every active negative check executes in a transaction that is
    unconditionally rolled back, so an unexpectedly permitted statement is reported as FAIL
    and leaves the database untouched. A wrong or privileged identity therefore never reaches
    CREATE, ALTER, DELETE or SET ROLE. Nothing here prints a row of tenant data.
    """
    results: list[ProbeResult] = []
    identity = probe_identity_gate(connection, role=role)
    results.append(identity)
    if not identity.passed:
        results.append(
            ProbeResult(PROBE_ABORTED, False, "identity gate failed; 0 active checks run")
        )
        return results
    artifacts = probe_artifacts(connection)
    results.append(artifacts)
    if not artifacts.passed:
        results.append(
            ProbeResult(PROBE_ABORTED, False, "artifact check failed; 0 active checks run")
        )
        return results

    def expect_refused(check: str, *statements: str) -> None:
        """PASS only when PostgreSQL refuses with insufficient_privilege (SQLSTATE 42501).

        Any other error is a failure too: a probe that "fails" for an unrelated reason (a
        syntax error, a transaction-block restriction) proves nothing about privilege. An
        unexpected success is a failure whose effect has already been rolled back.
        """
        outcome, code = execute_rollback_only(connection, *statements)
        if outcome == "refused":
            results.append(ProbeResult(check, code == INSUFFICIENT_PRIVILEGE, f"refused ({code})"))
        else:
            results.append(ProbeResult(check, False, "statement succeeded (rolled back)"))

    def expect_count(check: str, statement: str, expected: int) -> None:
        connection.rollback()
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
                value = _scalar_int(cursor.fetchone(), default=-1)
        except psycopg.Error as exc:
            results.append(ProbeResult(check, False, f"query failed ({exc.sqlstate})"))
            return
        finally:
            connection.rollback()
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
    expect_count(
        "tenants without tenant context discloses nothing",
        "SELECT count(*) FROM public.tenants",
        0,
    )
    expect_refused(
        "cannot read encrypted credentials directly",
        "SELECT count(*) FROM public.encrypted_credentials",
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
    expect_refused(
        "cannot call the private vault authorization predicate",
        "SELECT public.vault_credential_authorized('ring_pending_link', gen_random_uuid(), 'open')",
    )
    expect_count(
        "vault open of an unknown credential discloses nothing",
        "SELECT count(*) FROM public.vault_credential_open("
        "gen_random_uuid(), 'RING', 'ring_pending_link', gen_random_uuid()) WHERE outcome = 'ok'",
        0,
    )
    expect_refused(
        "cannot read edge node credentials directly",
        "SELECT count(*) FROM public.edge_node_credentials",
    )
    expect_count(
        "edge credential authentication of an unknown selector discloses nothing",
        "SELECT count(*) FROM public.authenticate_edge_node_credential("
        "gen_random_uuid(), decode(repeat('00', 32), 'hex'))",
        0,
    )
    expect_refused("cannot create tables", f"CREATE TABLE public.{PROBE_ARTIFACT} (id int)")
    expect_refused("cannot create roles", f"CREATE ROLE {PROBE_ARTIFACT} NOLOGIN")
    # CREATE DATABASE cannot run inside a transaction, so it can never be rolled back. The
    # privilege it needs is exactly rolcreatedb or SUPERUSER, both proven false by the gate
    # above; that catalog fact is the check, and no database is ever created by the probe.
    results.append(
        ProbeResult(
            "cannot create databases",
            True,
            "catalog: rolcreatedb=false superuser=false (never executed; not transactional)",
        )
    )
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
    expect_count(
        "own function defaults exclude PUBLIC EXECUTE",
        "SELECT count(*) FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole "
        "WHERE r.rolname = current_user AND d.defaclnamespace = 0 AND d.defaclobjtype = 'f' "
        "AND NOT EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a "
        "WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')",
        1,
    )
    if migration_role:
        # SET LOCAL ROLE is transaction-scoped; the rollback (and RESET ROLE) undoes it even
        # if the switch were permitted.
        expect_refused(
            f"cannot SET ROLE to {migration_role}",
            sql.SQL("SET LOCAL ROLE {}").format(_ident(migration_role)).as_string(),
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
            "plan/apply/verify connect with the ADMIN identity and inspect the role named by "
            "--role; probe must connect AS that runtime role (--url-ref to the API runtime DSN) "
            "and refuses, before any active check, when the connected identity is anything else."
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
            with psycopg.connect(psycopg_dsn(url), autocommit=False) as connection:
                results = probe(
                    connection, role=arguments.role, migration_role=arguments.migration_role
                )
                connection.rollback()
            failed = False
            for result in results:
                print(f"{'PASS' if result.passed else 'FAIL'}  {result.check}: {result.detail}")
                failed = failed or not result.passed
            if any(result.check == PROBE_ABORTED for result in results):
                print(
                    "probe refused: connect with the API runtime DSN "
                    "(--url-ref file:/run/secrets/api_database_url), never the admin one",
                    file=sys.stderr,
                )
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
