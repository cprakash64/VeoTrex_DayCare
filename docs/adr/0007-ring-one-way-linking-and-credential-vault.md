# ADR 0007: Ring one-way linking and credential vault

- Status: Accepted
- Date: 2026-08-23

## Context

Ring's supported default flow sends VeoTrex a one-use authorization code before the Ring user reaches
VeoTrex or selects an internal tenant. Tokens rotate on refresh, and Ring provides no documented
refresh idempotency key. The production cloud/KMS has not been selected.

## Decision

Use Ring-driven one-way linking. Represent credentials received before identity as provider-specific,
pre-tenant pending records accessed through revoked-by-default security-definer functions. Retrieve
only `/v1/users/me` Account ID. Authenticate and authorize the later browser claim, match the HMAC
nonce only on the server, atomically claim, then enforce POST `awaiting` -> internal `CONFIGURING` ->
PATCH `completed` -> `ACTIVE`.

Store tokens behind a narrow provider-neutral, context-bound, versioned `CredentialVault`. Use a
PostgreSQL row lock plus vault compare-and-swap for rotation. Treat uncertain one-use exchanges and
refreshes as explicit unhealthy states with no automatic replay. Use only an isolated in-memory vault
for automated tests/local development; production fails closed until a managed adapter is installed.

## Consequences

The flow handles partial failure without misreporting activation, prevents cross-tenant duplicate
bindings, and preserves future managed-vault portability. It requires operational reconciliation for
ambiguous provider results, periodic proactive refresh and stale-pending cleanup, and deployment-time
least-privilege database grants. Partner-Initiated OAuth is excluded unless Ring later provides
explicit invitation/allowlisting evidence, at which point this ADR must be revisited.
