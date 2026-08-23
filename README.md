# VeoTrex Childcare

Production-oriented foundation for the VeoTrex childcare safety platform. This repository
contains no camera integrations, computer vision, audio analysis, recording, alerts, attendance,
or other operational safety features. It is not legal advice and does not guarantee compliance.

## Prerequisites

- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and pnpm 11.19
- Docker with Compose

## Local setup

```bash
cp .env.example .env
make bootstrap
make db-up
make migrate
```

The `.env.example` values are local-only. Replace them through the deployment secret manager in
every non-local environment; never commit `.env`.

If port 5432 is already occupied, start PostgreSQL on another host port and update the database URL:

```bash
VEOTREX_POSTGRES_PORT=55432 make db-up
# set VEOTREX_DATABASE_URL to use localhost:55432 before running migrations or the API
```

Run each service in its own terminal:

```bash
make api   # http://127.0.0.1:8000/health/live
make web   # http://127.0.0.1:3000
make edge  # stop with SIGINT/SIGTERM
```

Run the full local quality gate with `make check`. Run migrations with `make migrate`, and stop
PostgreSQL with `make db-down`. `make db-down` preserves the named volume.

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
server-side binding. PostgreSQL RLS fails closed when `app.tenant_id` is absent. Production must use
a non-superuser, non-table-owner, `NOBYPASSRLS` runtime role; migration ownership and runtime access
are intentionally separate deployment concerns.

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
