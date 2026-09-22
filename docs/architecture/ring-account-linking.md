# Ring account linking and credential lifecycle

## Scope and flow

Stage 1B implements Ring's current Ring-driven, one-way account-linking flow. Partner-Initiated
OAuth is invitation-only and VeoTrex has no allowlisting evidence, so it is intentionally excluded.
The implementation follows the [Ring API documentation](https://developer.amazon.com/docs/ring/api-documentation.html):

1. Ring sends a one-time authorization code directly to
   `POST /v1/integrations/ring/token-exchange`.
2. The API exchanges it once, vaults both tokens, and calls only `GET /v1/users/me`. It retains the
   stable JSON:API `data.id`; name, email, phone, and all other attributes are discarded.
3. The credential becomes `UNCLAIMED` without a tenant. Ring redirects the browser to the VeoTrex
   link URL with `time` and `nonce`.
4. Auth0 authentication and `MANAGE_INTEGRATIONS` authorization are mandatory. GET only renders a
   tenant confirmation. A same-origin POST BFF performs the mutation.
5. The API validates time, matches the HMAC server-side against at most 100 recent, unexpired
   candidates, and atomically moves the winner from `UNCLAIMED` to `CLAIMING`.
6. App Integrations POST must return `awaiting` before a tenant `CameraProviderConnection` is created
   in `CONFIGURING`. App Integrations PATCH must return `completed` before it becomes `ACTIVE`.

Ring OAuth and Amazon Vision are server-to-server only. The browser cannot supply an Account ID or
tenant ID and never receives credentials, signing keys, vault references, candidate records, or raw
Ring errors. No device, webhook, media, camera-assignment, or streaming API is part of this stage.

## Partner API request contract (V1-01A-2)

Every JSON API request sends exactly the documented headers: `Authorization: Bearer <token>`
(the raw token text, no quoting or masking), `Accept: application/json` and
`Content-Type: application/json`, with a `VeoTrex-ControlPlane/<version>` product User-Agent.
The reference's own GET examples send `Content-Type` too, so `GET /v1/users/me` does as well;
it has no body and no query. Only `data.id` of the users document is retained; name, email and
phone attributes are discarded. `POST /v1/accounts/me/app-integrations` sends
`{"account_identifier", "nonce"}` and requires a JSON:API `app-integrations` resource with
`attributes.status = "awaiting"`; `PATCH` sends `{"account_identifier", "status": "completed"}`
and requires `attributes.status = "completed"`. The `account_identifier` is the obfuscated
partner account the Ring user sees: the signed-in actor's display name masked to its first and
last character (`C***y@veotrex`), or a masked actor id, never an email, subject or tenant id.
A non-2xx provider response yields a `ring_provider_request_failed` log event carrying only the
operation, category, HTTP status, the JSON:API error title or code and a gateway correlation id.

## Pre-tenant and tenant data

`ring_pending_links` has no tenant column. It contains an opaque ID, stable Ring Account ID when
known, opaque credential reference/version, access expiry, timestamps, safe failure category, and
explicit lifecycle state. PostgreSQL grants no table access to `PUBLIC`. Its fixed-search-path
`SECURITY DEFINER` functions also revoke execute from `PUBLIC`. Production provisioning must grant
only those function signatures to the least-privileged API runtime role, never direct table access.
The migration/admin role owns the objects. Candidate values remain inside the narrow repository and
are never serialized by a tenant route.

Tenant connections retain forced RLS and transaction-local `app.tenant_id`. They store only safe
metadata: provider, stable external Account ID, opaque vault reference/context, generation, access
expiry, link actor/time, health state, refresh time, and disconnect/archive time. A global partial
unique index prevents the same Ring Account ID from belonging to two active tenant connections;
conflicts do not disclose the existing tenant.

Pending states are `RECEIVED`, `UNCLAIMED`, `CLAIMING`, `RING_CONFIRMATION_UNCERTAIN`,
`RING_CONFIRMED_UNBOUND`, `CLAIMED`, `FAILED`, and `ARCHIVED`. Connection states are `CONFIGURING`,
`ACTIVE`, `REAUTH_REQUIRED`, `REFRESH_UNCERTAIN`, `DISCONNECTED`, and `ARCHIVED`. Database functions
validate pending transitions, and partial failures remain observable.

## Credential storage

`CredentialVault` provides context-bound `store_new`, `get`, compare-and-swap
`replace_if_version`, and `delete`. `InMemoryCredentialVault` is isolated, non-persistent, and only
selected for test/local/development. It is not production-capable. All other environments select a
fail-closed unavailable adapter.

Real customer linking requires a managed KMS/secret backend or equivalently hardened adapter with
authenticated encryption, access policy, key/version metadata, audit, backup/restore, and secure
deletion. No local encryption or custom crypto is represented as a KMS. Configuration carries opaque
secret references; tokens are `SecretStr`, absent from domain rows and audit metadata, and Ring client
errors contain only operation/category/status. OAuth bodies, response bodies, authorization headers,
codes, nonces, keys, and tokens must never be logged.

## Nonce, replay, and authorization

The nonce is `base64url_no_padding(HMAC-SHA256(key, "<timestamp_ms>:<ring_account_id>"))`, exactly
43 URL-safe characters, compared with `hmac.compare_digest`. Time is nonnegative milliseconds,
defaults to Ring's 600-second window, and has zero future tolerance. Candidate creation age, access
expiry, archive/state filters, and a 100-row cap further bound matching.

A stolen nonce alone is insufficient: Auth0 and tenant-owner integration permission are required.
The server derives tenant and Account ID. `start_ring_pending_claim` is a conditional atomic update,
so one concurrent claimant wins and replayed/in-progress records leave the candidate set. Viewer,
Safety Reviewer, and facility-scoped roles lack permission.

The account-link GET does not mutate Ring or VeoTrex state. The confirmation uses a same-origin POST
route, content-type/schema validation, the server-held Auth0 access token, and no browser-selected
tenant. The Next.js BFF's strict Origin check provides CSRF protection for this cookie-authenticated
browser step.

## Refresh concurrency and ambiguity

`expires_in` must be a sane bounded integer and determines access expiry; VeoTrex does not assume
four hours. Scope is deliberately accepted only as a space-separated string or string list. Ring's
approximately 30-day refresh-token lifetime is not stored as an exact expiry because the response
does not provide one.

Refreshing holds `SELECT ... FOR UPDATE` on the tenant connection, loads the matching vault
generation, sends exactly one request, compare-and-swaps both newly rotated tokens, then commits the
new generation and expiry. A contender waits, observes the new valid generation, and makes no Ring
call. Vault CAS prevents an old writer from overwriting a newer generation.

Authorization-code exchange and refresh have no automatic retry. A transport or server failure
after sending either one-use operation is ambiguous. Refresh commits `REFRESH_UNCERTAIN`, blocks
further credential use, and requires controlled investigation/re-link; it never resends the old
refresh token. A definitive rejected/malformed refresh moves to `REAUTH_REQUIRED` and creates a safe
audit event.

`due_for_proactive_refresh(tenant_id)` supplies a tenant-scoped sweep capability. Production must run
it periodically; Stage 1B introduces no queue or scheduler. Recommended low-cardinality metrics are
exchange/claim/refresh outcomes, ambiguous refresh count, Ring latency by operation/status class, and
active/reauth-required counts. IDs and credential identifiers must never be metric labels.

## Recovery and retention

- Exchange succeeded but `/users/me` failed: the credential remains safely vaulted in `RECEIVED`
  with a safe failure category. Never replay the authorization code. An operator may retry only
  `/users/me` before access expiry; expired records have their vault material deleted and are
  archived by `veotrex-ring-pending-expiry` (below). Nothing about it is Internet-exposed.
- App Integrations POST ambiguous: `RING_CONFIRMATION_UNCERTAIN`; never auto-repeat POST.
- POST succeeded but persistence failed: `RING_CONFIRMED_UNBOUND`; never report ACTIVE.
- PATCH failed: keep the connection `CONFIGURING`; the protected resume endpoint safely retries PATCH
  and activates only after a validated `completed` response.
- Disconnect: first mark `DISCONNECTED`/`DISABLED`, then delete local vault material and clear its
  reference. No undocumented Ring revocation API is called; local deletion is not remote revocation.

Operators must alert on all uncertain/unbound states and stale `RECEIVED` records. Before real
onboarding, install a managed vault, create distinct migration/runtime database roles, verify the
runtime role is non-owner `NOSUPERUSER NOBYPASSRLS`, grant only required functions/tables, configure
proactive refresh, and run the pending-link expiry job on a schedule.

### Expiry of abandoned pending links (V1-00A-PROD-R2)

`access_expires_at` bounds the **access** token only. The sealed record also holds the refresh
token, which Ring honours for roughly thirty days and which VeoTrex never uses for a pending
link, so an expired pending link is unclaimable yet still holds live provider authorization
material. Nothing in the request path removes it: the candidate query and
`start_ring_pending_claim` merely stop returning the row. The first real Ring attempts left
expired `RECEIVED` and `UNCLAIMED` rows, each with its credential, in production for days.

`veotrex-ring-pending-expiry` (`veotrex_api.ring_pending_expiry`) is the bounded, idempotent
maintenance command that converges them. It runs with the admin/migration identity as a
one-shot job, exactly like `migrate` and `runtime-role`, and refuses any role Row Level Security
applies to: the API runtime role keeps no privilege on `encrypted_credentials` or
`ring_pending_links`, the vault delete predicate keeps authorizing pre-tenant deletion only in
`RECEIVED`, and no grant changed. The age condition is `access_expires_at <= now()` on the
database clock, the exact complement of the claim predicate.

| State | Expired link |
|---|---|
| `RECEIVED`, `UNCLAIMED`, `FAILED` | credential row deleted, link `ARCHIVED` (`archived_at` set; account, failure category and timestamps preserved) |
| `CLAIMING` | never modified; counted for attention (ownership transaction may be in flight, Ring POST may have been sent) |
| `RING_CONFIRMATION_UNCERTAIN`, `RING_CONFIRMED_UNBOUND` | never modified; counted for attention (remote evidence) |
| `CLAIMED` | not examined; the credential belongs to the tenant connection |
| `ARCHIVED` | terminal; a credential still attached and owned by no connection is removed as an orphan |

A credential referenced by any `camera_provider_connections.credential_owner_id` is never
deleted, whatever the link state; a pre-claim link in that situation is reported as
`inconsistent` and left alone. `apply` is one transaction: candidates are locked with
`FOR UPDATE SKIP LOCKED`, credentials are deleted, links are archived, then commit, so two
workers never take the same row, a run that dies leaves nothing half-done, and a claim that
started first is already `CLAIMING` and out of reach. `dry-run` executes in a `READ ONLY`
transaction and takes no lock. Output is counts only (examined, per-state expired, archived,
credentials removed, already clean, skipped by state, inconsistent, remaining); exit status 3
signals rows that need operator attention. No identifier, reference, ciphertext, nonce, token
or DSN is ever printed.

## Hostile review

| Threat | Control |
|---|---|
| Anonymous stolen nonce | Auth0 plus `MANAGE_INTEGRATIONS` |
| Browser chooses tenant/Account ID | both derive server-side; absent from claim schema |
| Pending accounts enumerated | no PUBLIC grants and no browser serialization |
| Nonce replay/concurrent claim | eligible-state filter plus atomic transition |
| Cross-tenant reassociation | global partial unique index and generic conflict |
| Concurrent rotation | row lock plus vault generation CAS |
| Stale generation overwrite | CAS failure becomes unhealthy |
| Generic retry replays a secret | single request and explicit ambiguity state |
| Malformed JSON activates | strict response validation; `completed` required |
| POST succeeds/PATCH fails | durable `CONFIGURING` and protected resume |
| Viewer attaches account | permission denied at dependency and service layers |
| Disconnected token is used | state gate blocks use before deletion |
| Secret reaches logs/audits | secret types, opaque refs, safe taxonomy, no raw bodies |
