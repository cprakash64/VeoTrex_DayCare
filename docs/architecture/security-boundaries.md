# Security boundaries

## Tenant isolation and authorization

Every customer-owned table has a non-null `tenant_id`. Nested relationships use composite foreign
keys `(resource_id, tenant_id)`, preventing a camera, zone, area, provider connection, edge node,
assignment, actor, or audit record from crossing tenants. PostgreSQL row-level security is enabled
and forced on every customer-owned table. Policies compare `tenant_id` with the transaction-local
`app.tenant_id`; a missing context matches nothing. The application helper fails before setting RLS
when no tenant is bound.

Production must use a least-privileged runtime role that is neither superuser nor table owner and
does not have `BYPASSRLS`. A distinct migration role owns schema. RLS is defense in depth, not the
authorization system: future endpoints must authenticate an actor, authorize a role and resource,
bind the tenant, start a transaction, set its local tenant, then access data. Cross-tenant service
operations require a separately audited administrative path.

Roles are represented initially as an actor attribute, not a completed RBAC design. Role vocabulary,
facility-scoped grants, emergency access, and machine identity are open decisions.

## Secrets and encryption

Provider connection rows contain an opaque `secret_ref`, never credentials. Adapters will resolve
references through an approved secret manager inside their boundary. `.env` is ignored; examples
contain local-only values. CI scans Git history with gitleaks. Logs must never contain tokens,
credential objects, media payloads, child details, or unrestricted request bodies.

TLS is required for external and edge/cloud traffic in production. PostgreSQL connections must use
TLS outside isolated local development. Infrastructure must provide encrypted disks, backups, object
storage, and secret storage with managed keys and rotation; vendor selection remains open.

## Edge trust boundary

Edge nodes are untrusted network peers. Future configuration must be authenticated, integrity
protected, versioned, replay resistant, least-privilege scoped to assigned cameras, and signed or
delivered over mutually authenticated transport. Local secrets require hardware-backed protection
where supported. Edge compromise must not grant other-tenant or control-plane database access.

## Audit and data minimization

Audit events record actor, action, target identity, UTC time, request ID, and allowlisted structured
metadata. Arbitrary sensitive content is prohibited. Audit foreign keys use `RESTRICT`; operational
parents are archived instead of cascade-deleted. A future append-only/immutable evidence ledger must
add tamper evidence, retention locks, export verification, clock-quality records, and legal holds.

Child identity and biometric schemas are absent. Collection must be purpose-limited and minimized.
Video retention is independent from metadata and audit retention so deleting media cannot silently
erase its audit trail, and deleting metadata cannot implicitly delete evidence. Retention values are
not invented until counsel/authorized compliance owners approve them.
