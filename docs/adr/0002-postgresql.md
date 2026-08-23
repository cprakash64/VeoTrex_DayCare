# ADR 0002: PostgreSQL metadata store

## Context

VeoTrex needs relational ownership constraints, transactional audit metadata, migrations, durable
policy versions, and database-enforced tenant isolation.

## Decision

Use PostgreSQL 17 with SQLAlchemy 2 and Alembic. Use UUID keys, composite tenant foreign keys, row-level
security, restrictive deletes, JSON only for genuinely structured extension data, and UTC timestamps.
Raw media and credentials are excluded.

## Alternatives considered

SQLite is useful for isolated tests but lacks the production RLS and concurrency behavior. A document
database weakens relational tenant constraints. Additional caches/event stores are not required.

## Consequences

PostgreSQL becomes critical for readiness and requires backups, restore testing, TLS, role separation,
monitoring, and disciplined migrations. Local and CI tests need a PostgreSQL service.
