# ADR 0016: Local and test PostgreSQL isolation

- Status: Accepted (R5A-R3-V1-R1)
- Date: 2026-09-12

## Context

R5A-R3-V1 stopped before starting PostgreSQL and reported two blocking conditions. Inspection of
the committed configuration found four defects that had to be corrected before any database could
safely run on a developer machine or a Jetson sitting on a childcare LAN.

1. The development service published `"${VEOTREX_POSTGRES_PORT:-5432}:5432"`. Docker's short form
   has no host address, so it binds every interface. This host has a routable Wi-Fi interface and
   both `ufw` and `nftables` are inactive - and published-port DNAT rules bypass host `INPUT`
   filtering regardless, so a firewall was never the boundary.
2. The database suite targeted the same persistent development database the named volume holds.
3. Those tests perform **cluster-scoped** operations: `CREATE ROLE`, `DROP ROLE`, `DROP OWNED`,
   plus row deletion.
4. A literal PostgreSQL password was committed to a public repository, and a secret-scanning
   allowlist pinned that same value.

## Decision

### Two separate PostgreSQL clusters, not two databases

`postgres` keeps persistent development data. `postgres-test` is a **separate PostgreSQL server**
for automated tests, behind a `test` Compose profile, on its own loopback port, with `tmpfs`
storage and its own database, user and secret.

A `veotrex_test` database inside the development server would **not** have been sufficient.
PostgreSQL roles are cluster-wide: `DROP ROLE` executed from any database removes that role for
every database in the cluster. Isolating role-level operations therefore requires a separate
server process, which is the single most important property in this decision.

### Loopback-only publication

Both services publish as `127.0.0.1:<port>:5432`. The development cluster keeps its default 5432;
the test cluster uses 55433, so the two are distinguishable by port alone. A static test fails if
either is ever republished on a wildcard address.

### No database password in source

Neither service carries a password. Both read `POSTGRES_PASSWORD_FILE` from a git-ignored file
under `infra/local/secrets/`, created by the operator on the host. A missing file fails the
service closed; there is deliberately no `${VAR:-default}` fallback, because a source-controlled
fallback is a committed credential by another name. Development and test credentials are
independent. Continuous integration sources its password from a repository secret and fails
closed if that secret is absent.

### The previously committed password is compromised

`PUBLIC_COMMITTED_POSTGRES_PASSWORD_STATUS = COMPROMISED_DO_NOT_REUSE`

It existed in a public repository and must never be used again, for any environment. History is
deliberately **not** rewritten: rewriting proves nothing about copies already taken, and safety
comes from never reusing the value rather than from hiding it. The secret-scanning allowlist entry
that pinned it has been removed - it both preserved the value and would have suppressed detection
of it - and `infra/local/compose.yaml` is no longer path-exempt, so a re-added literal is reported.

### Explicit, guarded test target

Destructive tests resolve `VEOTREX_TEST_DATABASE_URL` and never inherit `VEOTREX_DATABASE_URL`.
`veotrex_api.database_targets` refuses a target that names a development or system database, whose
name does not end in `_test`, whose host is not loopback, or - decisively - whose cluster identity
`(host, port)` equals the development target's, since that means shared roles. When nothing is
configured the suite receives an unreachable sentinel, so it fails to connect rather than reaching
development data. A configured-but-unsafe target raises instead of running.

### Workspace-safe commands

Every project command uses `uv run --all-packages`. `uv run --package <member>` re-synchronises the
shared workspace virtualenv down to one member's closure and uninstalls the other member's
dependencies (NumPy, SciPy, Pillow). Run before a database suite it produces mass collection errors
that look like database failures. Recovery is
`uv sync --frozen --all-packages --group qualification`.

## Consequences

Development and automated tests can no longer collide: a destructive test cannot drop a role the
development database depends on, and cannot reach a non-loopback host. Local setup now requires one
manual step - creating two secret files - before the first database start, which is the intended
cost of removing committed credentials. Docker socket ownership, group membership and daemon
configuration are unchanged by this decision; no package was installed and no container was started.

Next gate: isolated live PostgreSQL qualification - start the two clusters, run the migration chain
through 0005 against the test cluster, and execute the previously blocked database suites.
