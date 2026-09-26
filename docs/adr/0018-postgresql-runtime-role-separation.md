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
Since V1-01A-2-R1 `probe` fails closed: a read-only identity gate (connected role equals
`--role`, every restricted attribute, no ownership or membership) and an artifact guard run
before any active check; every active negative check is executed in a transaction that is
rolled back unconditionally, and `CREATE DATABASE` is asserted from the catalog rather than
executed. Running `probe` with the admin DSN therefore aborts after the gate and mutates nothing.

### Privilege inventory

The runtime role holds `SELECT`/`INSERT`/`UPDATE` on the tenant tables the service code writes,
`SELECT` only on `tenants` (behind a self-only RLS policy), `actors` and `role_assignments`,
`SELECT`+`UPDATE` on `actor_identities`, and `SELECT`+`INSERT` on `audit_events` (append-only
from the runtime) and `provider_events`. It holds **no** `DELETE` on any tenant table and **no**
table privilege at all on `encrypted_credentials`, `ring_pending_links`, `ring_webhook_inbox`,
`tenant_identity_bindings`, `alembic_version`, the policy catalog, or tables the API does not yet
use (`facilities`, `areas`, `zones`, `edge_nodes`, `camera_assignments`). It can `EXECUTE` exactly
the fifteen SECURITY DEFINER functions the code calls (identity resolution, the pending-link state
machine, the webhook inbox, and the four vault operations); every other function, including the
private vault authorization predicate, is revoked. The classification below is the source of
truth and a unit test pins it to the ORM metadata.

### Fail-closed defaults (corrected in V1-00A-R1)

The first version of this decision installed `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT,
UPDATE ON TABLES TO veotrex_api`, so a table created by any future migration - teacher biometric
templates, enrollment images, integration secrets - would have been readable and writable by the
API automatically. That was the wrong default and is withdrawn. **No default privilege ever grants
the runtime role anything.** A new table, function or sequence is inaccessible to the API until a
developer classifies it in `veotrex_api.runtime_role.TABLE_CLASSIFICATION` (or lists a function
in `RUNTIME_FUNCTION_GRANTS`) and `apply` is re-run; a unit test fails CI when an ORM table has no
classification. `apply` converges: it revokes runtime access on every unclassified table, every
sequence, every non-allow-listed function, and on default-privilege entries an earlier version
installed, so an over-privileged deployment is corrected rather than merely extended.

The only defaults installed are *denials*: `ALTER DEFAULT PRIVILEGES FOR ROLE <migration role>
REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` (global, because per-schema defaults are added to the
global defaults and cannot remove PUBLIC's built-in EXECUTE - verified on PostgreSQL 17 and pinned
by a test that creates a function as the migration role), plus explicit no-op revokes for tables
and sequences that state the intent. Tests create a `future_sensitive_table`, a function and a
sequence as the migration role and prove the runtime is refused every privilege until a
deliberate classification grants exactly the intended ones.

The same global function-default revoke is applied for the runtime role *itself*
(V1-00A-PROD-R1). It cannot create functions today, but if it ever gains creation rights in some
schema, what it creates must not become PUBLIC-executable either; `verify` checks both creator
roles' defaults and refuses to infer the migration role when connected as the runtime role.

### Classification model

| Class | Privileges | Tables |
|---|---|---|
| RUNTIME_READ | SELECT | tenants (self-policy), actors, role_assignments |
| RUNTIME_WRITE | explicit set, DELETE never inferred | actor_identities (S,U); camera_provider_connections, cameras, camera_provider_devices, camera_provider_components (S,I,U) |
| RUNTIME_APPEND_ONLY | SELECT, INSERT | provider_events, audit_events |
| FUNCTION_ONLY | none | encrypted_credentials, ring_pending_links, ring_webhook_inbox, tenant_identity_bindings |
| RUNTIME_NO_ACCESS | none | alembic_version, jurisdiction_policies, policy_versions, facilities, areas, zones, edge_nodes, camera_assignments |

Later stages reclassify through `TABLE_CLASSIFICATION` in `runtime_role.py`, which is the
authoritative list. V1-04A (ADR 0024): `facilities` and `zones` became RUNTIME_READ; `areas` and
the new `classroom_ratio_policies` became RUNTIME_WRITE (S,I,U). DELETE is still never granted.
V1-04B (ADR 0025): `classroom_presence_snapshots` is RUNTIME_WRITE (S,I,U); a trigger limits
UPDATE to one revocation, and there is no DELETE. V1-04C (ADR 0026): `staff_ratio_eligibility` is
RUNTIME_WRITE (S,I,U) and `staff_presence_events` is RUNTIME_APPEND_ONLY (S,I); neither has DELETE,
and no function was added.

### The credential boundary (V1-00A-R1)

`encrypted_credentials` is global by design and the API holds the vault master key, so direct
table privileges would let a compromised API process decrypt every tenant's Ring tokens with one
`SELECT`. The runtime now has **no table privilege** on it. Migration 0006 adds four SECURITY
DEFINER functions - `vault_credential_create`, `_open`, `_replace`, `_delete` - each addressing
exactly one credential by primary key, checking the `(provider, owner_kind, owner_id)` binding,
and authorizing against existing state: the pending link being `CLAIMING` by the caller's tenant
(the claim step), a `camera_provider_connections` row of the caller's tenant referencing the
credential (retrieval, refresh, disconnect, remote removal), or, for deletion only, a link still
in `RECEIVED` or never created (token-receipt clean-up). The caller's tenant is `app.tenant_id`,
which `EncryptedCredentialVault` presents from the new optional `CredentialContext.tenant_id`.
The private predicate `vault_credential_authorized` is not executable by the runtime. No function
enumerates, and none takes anything but a primary key.

`tenants` gains `FORCE ROW LEVEL SECURITY` with a self-only policy, so a tenant-scoped request
reads its own row and nothing else; identity bootstrap is unaffected because the resolver is
SECURITY DEFINER and provisioning runs as the admin.

### Fail closed in the application

`veotrex_api.db.verify_runtime_role` reads `pg_roles` for `current_user` at startup. A superuser or
`BYPASSRLS` role, a role that can `CREATE` in the application schema, that owns any application
relation, or that is a member of another role raises `PrivilegedDatabaseRole` and the process
does not start; an unreachable
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
Remaining for a later stage: a migration identity distinct from the bootstrap superuser. When
that happens the SECURITY DEFINER functions will run as a non-superuser owner, and because
`tenants` and the tenant tables `FORCE` RLS, `resolve_tenant_identity_binding` and the vault
predicate will need an explicit bypass (owner `BYPASSRLS`, or policies granting the owner) -
which is exactly the kind of decision that separation stage must make deliberately.
