# Identity and access boundary

## Trust boundary

VeoTrex delegates interactive authentication to Auth0. Auth0 is not the source of tenant
ownership or application authorization. The API accepts an access token only after verifying its
signature, asymmetric algorithm allowlist, exact issuer, audience, expiration, optional not-before
time, subject, and Auth0 organization context.

The request path is:

1. Auth0 issues an API access token containing `sub` and stable `org_id` claims.
2. `Auth0IdentityVerifier` validates it against a bounded JWKS cache and emits a provider-neutral
   `ExternalIdentity`.
3. A database security-definer function performs one exact, active mapping from provider, issuer,
   and external organization ID to an internal Tenant UUID.
4. That server-resolved UUID is transaction-locally assigned to `app.tenant_id`.
5. Under RLS, an active `ActorIdentity` maps the tenant, provider, issuer, and subject to an active
   Actor. Active role assignments produce an `AuthenticatedPrincipal`.
6. Central permission helpers authorize the operation. Tenant queries execute in the same
   transaction, with PostgreSQL RLS as the final isolation boundary.

A body, query parameter, path value, cookie, or header containing a Tenant UUID is not
authorization evidence and never supplies `app.tenant_id`.

## External and internal identity

An Auth0 Organization is not a VeoTrex Tenant. It is signed external authentication context that
must be explicitly mapped to the authoritative internal Tenant record. Display names, email
addresses, and email domains are not security identifiers.

`TenantIdentityBinding` stores provider, exact issuer, and the provider's stable external
organization ID. `ActorIdentity` stores provider, exact issuer, and OIDC subject. Core Actor and
Tenant records contain no Auth0-specific columns. Archived bindings fail closed. The same OIDC
subject may have separately authorized memberships in multiple Tenants; each remains tenant-owned
and independently scoped.

The organization binding is the single deliberate RLS exception because it is needed before a
Tenant context exists. Runtime roles have no direct privileges on this table. They may execute only
the fixed-search-path, exact-match resolver function, which returns one active Tenant UUID or null.
Applying tenant RLS to this pre-context lookup would be circular or require a superuser/BYPASSRLS
function owner. Actor identities, role assignments, and all operational tenant data use forced RLS.

Stage 0 Actor identity and role columns are migrated into provider `legacy` bindings and recognized
roles, then removed. Legacy bindings cannot satisfy Auth0 resolution and preserve historical
Actor/AuditEvent identity.

## Authorization

Authentication and authorization are separate. A valid Auth0 user receives no VeoTrex access
until an active Actor identity and at least one active role assignment exist. There is no
first-seen owner rule, email-domain rule, or implicit just-in-time grant.

| Role | Scope | Permissions |
| --- | --- | --- |
| `TENANT_OWNER` | Tenant only | All Stage 1A permissions, including tenant configuration, facilities, integrations, and members |
| `FACILITY_ADMIN` | Tenant or assigned facility | Read operations, administer facility, configure future cameras, review safety data |
| `SAFETY_REVIEWER` | Tenant or assigned facility | Read operations and review safety data |
| `VIEWER` | Tenant or assigned facility | Read operations only |

Roles map centrally to explicit `Permission` values. Facility scope is a relational foreign key,
not JSON. All actor, creator, and facility references use composite tenant foreign keys. Active
assignments have scoped unique indexes and revocation is archival rather than deletion.

## API and JWKS behavior

The API uses PyJWT for JOSE verification and HTTPX for bounded JWKS retrieval. It does not
implement cryptography. JWKS entries are cached for the configured TTL (default five minutes). A
new key ID forces an immediate refresh for key rotation. When the cache expires, Auth0 must be
reachable; stale signing keys are not trusted indefinitely. Fetch failure, invalid JWKS, unknown
key ID, disallowed algorithm, and claim-validation failures fail closed.

After one unknown-key refresh, further forced refreshes are held for the configured cooldown
(default ten seconds), preventing random-key-ID traffic from amplifying outbound Auth0 requests.
The normal cache expiry path remains independent.

| Failure | Status | Behavior |
| --- | --- | --- |
| Missing/malformed/expired token, signature failure, wrong issuer/audience, missing organization or subject | 401 | Generic `authentication required`; bearer challenge |
| Unmapped/archived organization, missing/archived Actor identity, disabled Actor, no active role | 403 | Generic `access denied` |
| Auth0/JWKS unavailable without an unexpired usable cache | 401 | Fail closed; no stale-key fallback |
| New signing key | — | Unknown `kid` triggers refresh and normal signature/claim validation |

Logs contain a request ID and failure category only. Authorization headers, JWTs, tokens, JWKS
bodies, subjects, and organization IDs are not logged. Per-process failure logging is bounded to 20
events per minute, and production ingress must also rate-limit repeated authentication failures.
The application does not create a database audit event for every login or request.

## Web session and token architecture

The Next.js 16 App Router uses the official Auth0 Next.js SDK v4 and its `proxy.ts` handler for
login, callback, logout, rolling session, and back-channel logout routes. The public landing page
offers login. `/app` checks the encrypted server-side session and redirects unauthenticated users
to `/auth/login`.

The application shell retrieves its API access token only on the server and calls `GET /v1/me` as
a token-mediating backend. The SDK's browser `/auth/access-token` and account-connect endpoints are
disabled. UI components receive only the safe `/v1/me` response; raw sessions and tokens are never
rendered.

Auth0 must issue the VeoTrex API audience and require organization context for this B2B
application. Login organization selection happens at Auth0; the API still treats only signed
`org_id` as external context and internal mapping as authority.

## Initial owner bootstrap

There is no public bootstrap endpoint. Run the CLI with a deployment-administrator database
credential from a controlled environment:

```shell
uv run --package veotrex-api veotrex-provision bootstrap-owner \
  --tenant-id 00000000-0000-0000-0000-000000000001 \
  --issuer https://your-auth0-domain.example/ \
  --organization-id org_example \
  --subject 'auth0|example' \
  --display-name 'Initial owner' \
  --dry-run
```

Remove `--dry-run` only after review. The command validates an active Tenant, exact HTTPS issuer,
and absence of ambiguous organization/subject bindings. It atomically creates the organization
binding, Actor, Actor identity, owner role, and three safe AuditEvents. Duplicates are refused
without mutation. It accepts and prints no tokens or secrets.

The CLI must use a migration/deployment admin credential with RLS bypass. The production API
credential must be a non-owner, non-superuser, `NOBYPASSRLS` role granted only needed table
privileges plus:

```sql
GRANT EXECUTE ON FUNCTION resolve_tenant_identity_binding(text, text, text)
TO your_veotrex_runtime_role;
```

The migration revokes this function from `PUBLIC`. The function has a fixed search path, returns
only an active Tenant UUID, and performs exact matches. Never give the API the deployment-admin
credential.

## MFA and operational requirements

These are Auth0/deployment configuration requirements, not controls the current application can
attest from an access token:

- Require MFA for every `TENANT_OWNER` and `FACILITY_ADMIN`.
- Delegate password, breached-password, bot detection, and session security policy to Auth0.
- Require verified email when email is contact data, but never authorize from it.
- Configure short access-token lifetime, bounded rolling/application sessions, back-channel
  logout, refresh-token rotation/reuse detection when refresh tokens are enabled, and
  incident-driven user/session revocation.
- Restrict Auth0 administration with MFA, least privilege, audit review, and separate
  production/non-production tenants.

Emergency access and application-verifiable MFA/step-up claims remain explicit future decisions.

## Migration and recovery

Migration `0002_identity_access` is intentionally not downgradeable. A downgrade would collapse
multiple provider identities and scoped/historical assignments into Stage 0 columns and silently
destroy authorization history. Recovery uses a tested database backup/restore or a forward
corrective migration.

Health endpoints remain public. Future provider webhook and token-exchange endpoints will use
their own authentication. No Ring model, credential, OAuth flow, request, or webhook is introduced
in Stage 1A.
