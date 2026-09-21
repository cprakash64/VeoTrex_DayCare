# ADR 0018: PostgreSQL runtime role separation

- Status: Accepted (V1-00A)
- Date: 2026-09-21

## Context

The V1 Stage 0 audit found that the deployed API connected to PostgreSQL as `veotrex`, the
`POSTGRES_USER` the official image bootstraps as a superuser. Every migration since 0001 enables
and forces Row Level Security with a `tenant_isolation` policy keyed on `app.tenant_id`, and the
identity and Ring documents prescribe a `NOSUPERUSER NOBYPASSRLS` runtime role - but nothing in
the repository or the deployment ever created one. Superusers, and any role with `BYPASSRLS`, are
exempt from RLS regardless of `FORCE ROW LEVEL SECURITY`. Tenant isolation therefore rested on
the explicit `tenant_id` predicates in application queries alone, and the RLS suite in CI ran as
the CI superuser, which could not have noticed.

## Decision

### Three identities

| Identity | Role | Used by | Never used by |
|---|---|---|---|
| bootstrap / admin | `POSTGRES_USER` (`veotrex`), superuser | role provisioning, backup, restore | the API |
| migration | today the same role as bootstrap | `alembic upgrade`, the `migrate` job | the API |
| runtime | `veotrex_api`, `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT` | the API process only | migrations, provisioning, backups |

Keeping the migration identity equal to the bootstrap identity is a deliberate, bounded scope
decision: separating them means transferring ownership of every table and SECURITY DEFINER
function, which changes what those functions execute as. That is a later stage; the property
this decision establishes is that **the API is not the owner and is subject to RLS**.

### Provisioning is administration, not a migration

Cluster-level role DDL does not belong in Alembic: migrations describe the application schema,
run in the migration identity's transaction, and would have to embed a deployment-specific role
name and credential path. `veotrex-db-runtime-role` (`veotrex_api.runtime_role`) is a privileged
console script in the same spirit as `veotrex-provision`. `apply` is idempotent: role attributes
are re-asserted on every run, each grant is revoked-then-granted so the effective set converges
exactly, database access is `CONNECT` only, schema access is `USAGE` only, and the password is
changed only when `--password-ref` names a secret. The password reaches PostgreSQL as a
pre-computed SCRAM-SHA-256 verifier so the plaintext never enters server logs. All statements run
in one transaction; a failure leaves the cluster unchanged. `verify` audits the effective model
from catalog metadata and `probe` connects *as* the runtime role and proves the boundary from the
inside (no rows without context, no direct access to function-only tables, no DDL, no role or
policy changes, no `SET ROLE` to the admin, `row_security = off` refused rather than honoured).

### Privilege inventory

The runtime role holds `SELECT`/`INSERT`/`UPDATE` on the tenant tables the service code writes,
`SELECT` only on `tenants`, `actors` and `role_assignments`, `SELECT`+`UPDATE` on
`actor_identities`, `SELECT`+`INSERT` on `audit_events` (append-only from the runtime) and
`provider_events`, and `SELECT`/`INSERT`/`UPDATE`/`DELETE` on `encrypted_credentials` (the vault
deletes rows on disconnect). It holds **no** `DELETE` on any tenant table and **no** access at all
to `ring_pending_links`, `ring_webhook_inbox`, `tenant_identity_bindings`, `alembic_version`, the
policy catalog, or tables the API does not yet use (`facilities`, `areas`, `zones`, `edge_nodes`,
`camera_assignments`). It can `EXECUTE` exactly the eleven SECURITY DEFINER functions the code
calls; every other function is revoked. A unit test pins the classification to the ORM metadata,
so adding a table forces an explicit decision.

### Default privileges keyed to the creating role

`ALTER DEFAULT PRIVILEGES FOR ROLE <migration role> IN SCHEMA public GRANT SELECT, INSERT, UPDATE
ON TABLES TO veotrex_api` makes tables created by future migrations usable without a remembered
grant, while `DELETE` and function `EXECUTE` stay explicit allowlist decisions applied by re-running
`apply` after the migration. PostgreSQL scopes default privileges to the *creating* role, not the
schema, which is why the migration identity is named explicitly and verified. On PostgreSQL 17 a
bare `REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` default stores nothing; pairing it with an explicit
`GRANT EXECUTE ... TO <migration role>` persists the PUBLIC-free default, and a test creates a
table and a function as the migration role to prove both behaviours.

### Fail closed in the application

`veotrex_api.db.verify_runtime_role` reads `pg_roles` for `current_user` at startup. A superuser or
`BYPASSRLS` role raises `PrivilegedDatabaseRole` and the process does not start; an unreachable
database only defers the check. `/health/ready` repeats the check on every call and reports
`reason: privileged_database_role` with HTTP 503, so `docker compose up --wait` fails the rollout
instead of serving traffic. The diagnostic names the role and attribute, never the DSN. There is no
environment flag that disables this: the automated suite provisions a real restricted role
(`veotrex_api_test`) from the admin identity at session start and runs every application-level
test as that role.

### Separable DSNs

`VEOTREX_DATABASE_URL[_REF]` is the API's runtime DSN. `VEOTREX_MIGRATION_DATABASE_URL[_REF]` is
read only by Alembic and falls back to the runtime DSN when unset, so a deployment that already
hands the admin DSN to a one-shot migration job is unchanged. On Hostinger the `api` service now
mounts only `api_database_url`; `migrate` and the new `runtime-role` job mount `database_url`
(admin) and the latter also `api_database_password`.

## Alternatives rejected

- Granting `BYPASSRLS`, object ownership, or membership in the admin role to the runtime role.
- Disabling or un-forcing RLS and relying on application predicates.
- An environment switch that skips the startup guard outside production.
- Creating the role from an Alembic migration.
- Blanket `GRANT ALL ON ALL TABLES`; `GRANT EXECUTE ON ALL FUNCTIONS`.

## Consequences

Cross-tenant disclosure now requires defeating PostgreSQL RLS itself rather than a missed `WHERE`
clause. Deployments gain two secret files and one idempotent job in the deploy order, and every
migration that adds a function or needs `DELETE` must be followed by `runtime-role apply`.
Remaining for a later stage: a migration identity distinct from the bootstrap superuser, and a
review of whether `tenants` should carry an RLS policy of its own.
