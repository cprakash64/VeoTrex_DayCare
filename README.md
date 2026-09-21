# VeoTrex Childcare

Production-oriented foundation for the VeoTrex childcare safety platform. This repository
contains no camera integrations, computer vision, audio analysis, recording, alerts, attendance,
or other operational safety features. It is not legal advice and does not guarantee compliance.

## Prerequisites

- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and pnpm 11.19
- Docker with Compose

## Local setup

Create the two local database secrets first. No database password is committed, so the containers
fail to start until these files exist. Generate them on your own machine; never commit them, never
paste them into a chat tool, and never pass them as command-line arguments:

```bash
mkdir -p infra/local/secrets && chmod 700 infra/local/secrets
umask 077
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > infra/local/secrets/postgres_password
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > infra/local/secrets/postgres_test_password
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > infra/local/secrets/postgres_api_password
chmod 600 infra/local/secrets/postgres_password infra/local/secrets/postgres_test_password \
  infra/local/secrets/postgres_api_password
```

`postgres_api_password` is the credential of the restricted `veotrex_api` role the API process
connects as. It is distinct from the bootstrap password on purpose: the bootstrap role is a
PostgreSQL superuser, and superusers are exempt from Row Level Security.

Then copy `.env.example` to `.env` and replace each `REPLACE_WITH_*` placeholder with the matching
value you just generated. `infra/local/secrets/` and `.env` are both git-ignored.

```bash
cp .env.example .env
make bootstrap
make db-up
make migrate           # as the bootstrap/migration identity (VEOTREX_MIGRATION_DATABASE_URL)
make db-runtime-role   # create/refresh the restricted veotrex_api role the API connects as
```

The `.env.example` values are placeholders only. Replace them through the deployment secret manager
in every non-local environment; never commit `.env`.

Both clusters publish on loopback only. Override either host port if it is already occupied, and
update the matching URL before running migrations or the API:

```bash
VEOTREX_POSTGRES_PORT=55432 make db-up
# set VEOTREX_DATABASE_URL to use 127.0.0.1:55432
```

Run each service in its own terminal:

```bash
make api   # http://127.0.0.1:8000/health/live
make web   # http://127.0.0.1:3000
make edge  # stop with SIGINT/SIGTERM
```

Run the full local quality gate with `make check`. Run migrations with `make migrate`, and stop
PostgreSQL with `make db-down`. `make db-down` preserves the named volume.

### Development and test databases are separate clusters

Development data lives in the `postgres` service (persistent named volume, port 5432). Automated
database tests use `postgres-test`: a **separate PostgreSQL server** behind the `test` profile, on
port 55433, with disposable `tmpfs` storage and its own credential.

They are two servers rather than two databases because the database suite performs cluster-scoped
operations — `CREATE ROLE`, `DROP ROLE`, `DROP OWNED`. PostgreSQL roles are cluster-wide, so a
`veotrex_test` database inside the development server would still let a test drop roles the
development database depends on.

```bash
make db-test-up      # start only the disposable test cluster
make migrate-test    # migrate it, after validating the target
make db-test         # run the database suite against it
make db-test-down    # discard it
```

Destructive tests read `VEOTREX_TEST_DATABASE_URL` and never inherit `VEOTREX_DATABASE_URL`. The
target is refused unless it names a database ending in `_test`, on a loopback host, on a cluster
distinct from development. With nothing configured the suite gets an unreachable target and simply
fails to connect, so it can never reach development data.

Always invoke uv as `uv run --all-packages`. `uv run --package <member>` re-synchronises the shared
workspace virtualenv down to one member and uninstalls the other member's dependencies; recover
with `uv sync --frozen --all-packages --group qualification`.

## Repository boundaries

- `apps/api`: FastAPI control plane, domain persistence, policy validation, provider contracts
- `apps/web`: minimal Next.js application shell
- `services/edge-agent`: hardware-neutral lifecycle skeleton
- `config/jurisdictions`: versioned policy packs kept out of detection logic
- `infra/local`: development PostgreSQL only
- `docs`: architecture and decision records

See [system overview](docs/architecture/system-overview.md),
[security boundaries](docs/architecture/security-boundaries.md),
[identity and access](docs/architecture/identity-and-access.md), and
[domain model](docs/architecture/domain-model.md).

## Database and tenant context

Alembic owns schema changes. Tenant-owned transactions must call
`apply_tenant_to_transaction(session)` after resolving an authenticated tenant through a trusted
server-side binding. PostgreSQL RLS fails closed when `app.tenant_id` is absent.

Three database identities are kept apart (ADR 0018). The bootstrap superuser (`POSTGRES_USER`)
and the migration identity provision and migrate; the API connects only as the restricted
`veotrex_api` role - `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
NOINHERIT`, owning nothing and holding only the table and function privileges enumerated in
`veotrex_api.runtime_role`. Nothing is granted by default: a new table, function or sequence
is inaccessible to the API until it is classified there and `apply` is re-run, and CI fails on an
unclassified ORM table. `veotrex-db-runtime-role apply` provisions and converges it, `verify`
audits it, and `probe` exercises the boundary from the runtime role's own connection. The API
refuses to start, and readiness reports `privileged_database_role`, if it is ever connected as a
superuser or `BYPASSRLS` role. The automated suite runs application code as a role provisioned
the same way (`veotrex_api_test`) and uses the admin identity only to seed and inspect.

## Identity bootstrap

Auth0 performs external authentication; internal Tenant, Actor, role, and RLS records remain
authoritative. Never put Auth0 secrets in source files. The privileged first-owner CLI, production
MFA requirements, runtime database grants, and failure behavior are documented in
[identity and access](docs/architecture/identity-and-access.md).

## Ring account linking

Ring uses its Ring-driven one-way account-linking flow, a pre-tenant pending credential boundary,
and a versioned credential-vault abstraction. Local/test uses only a non-persistent in-memory vault;
production fails closed until a managed vault adapter is configured. See
[Ring account linking](docs/architecture/ring-account-linking.md) and
[ADR 0007](docs/adr/0007-ring-one-way-linking-and-credential-vault.md).

## Stage boundary

Stage 1B adds only secure Ring account linking and credential lifecycle. Ring devices, webhooks,
streaming, inference, recording, and alerts remain deliberately absent.
