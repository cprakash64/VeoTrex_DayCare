# ADR 0015: Ring credential vault and public HTTPS control-plane origin

- Status: Accepted (R5A-R3)
- Date: 2026-09-12

## Context

R5A-R2 cleared the WebRTC media runtime, leaving the real-Ring blockers in the control plane: no
production credential vault, no public HTTPS origin, no developer credentials, and no linked
account. R5A-R3 prepares the control plane so real credentials can be introduced safely in a later
stage. It introduces no Ring credentials and opens no camera.

The current official Ring contract was re-verified on 2026-09-12 before anything was changed.
One-way account linking is still the documented, recommended Ring-driven model, so the existing
flow is kept and **not** silently migrated to Partner-Initiated OAuth. Re-confirmed: token endpoint
`https://oauth.ring.com/oauth/token` with `authorization_code` and `refresh_token`; ~60 s
authorization codes, ~4 h access tokens, ~30 d refresh tokens; **each refresh returns a new
refresh token that invalidates the old one**; webhook signatures are HMAC-SHA256 over the raw body
in the `X-Signature` header with a `sha256=` hex prefix; account-link nonces are HMAC-SHA256 over
`<time>:<account_id>`, URL-safe Base64 unpadded, valid for 600 s; app integration requires
POST `awaiting` then PATCH `completed`; staging and production are configured on separate portal
tabs whose values do not carry over.

## Decision

### Trust boundary

Ring callbacks terminate on the **cloud control plane** (FastAPI + Next.js + PostgreSQL), which
ADR 0005 already designates as the home of tenant identity, inventory, authorization and audit. The
Jetson edge node keeps camera connectivity and media. The Jetson is deliberately **not** exposed to
the Internet to satisfy Ring's HTTPS callback requirement: doing so would put a device sitting on a
childcare LAN, holding decoded video, behind an inbound public port. No firewall or router change
is made by this stage.

### Credential classes

Static application secrets (Client Secret, HMAC Signing Key) stay behind `SecretResolver`
references and are never stored in the database. Dynamic user credentials (access token, refresh
token, expiry, generation) are stored encrypted. The Client ID is not secret but remains
configuration rather than a literal in source.

### Production vault

`EncryptedCredentialVault` implements the **existing** `CredentialVault` protocol, so
`RingLinkService` and `RingWebhookService` are unchanged and no second credential system exists.
Records are sealed with **AES-256-GCM** from `cryptography`, which was already resolved in
`uv.lock` via `pyjwt[crypto]`; the dependency edge is now declared explicitly on `veotrex-api`
because security-critical code should not rest on a transitive edge. Locking changed only that
edge — no package version moved. No cryptography is implemented in this repository.

Associated data binds each ciphertext to its non-secret context with a canonical, separator-checked
encoding of `(schema version, provider, owner kind, owner id, credential version)`. A ciphertext
copied to another provider, owner, or version fails authentication instead of silently decrypting.

Vault records are **not** tenant-scoped, and that is deliberate: the `CredentialContext` contract is
`(provider, owner_kind, owner_id)`, and Ring one-way linking necessarily stores a credential
*before* any tenant is known. Isolation therefore comes from AEAD context binding plus narrow
grants (`REVOKE ALL ... FROM PUBLIC`), not from a tenant column that does not exist at that point
in the lifecycle. Tenant-scoped tables keep the existing RLS conventions.

### Master key

The key is supplied as a **reference** (`vault_master_key_ref`) resolved through the existing
`SecretResolver`, so the value never enters settings objects, Git, migrations, fixtures, or argv.
It is never printed, logged, hashed, or fingerprinted — a key-derived identifier would help an
offline attacker. Without a usable 32-byte key the application selects the fail-closed adapter
rather than starting with unprotected credential storage. Python cannot guarantee memory
zeroisation; this design minimises how long plaintext is referenced but makes no erasure claim.

### Rotation

`replace_if_version` is compare-and-swap under `SELECT ... FOR UPDATE`. A worker holding
generation *n* cannot overwrite a credential already rotated to *n+1*, so an old refresh token can
never replace a newer one — the failure mode that would otherwise silently break Ring's rotating
refresh tokens and force a re-link.

### Public origin and callbacks

One configured origin (`public_origin`) is the single source of truth: HTTPS only, a fully
qualified hostname, optional port, and nothing else — no userinfo, path, query, fragment, wildcard,
IP literal, loopback, private address, or `.local`. The four Ring callback URLs are derived from
that origin plus **constant application-owned routes** verified against the actual routes in this
repository. Nothing request-controlled participates: callbacks are never built from `Host` or
`X-Forwarded-Host`, because a forged header would otherwise relocate Ring's callbacks to an
attacker's host. TLS terminates at a proxy the operator controls; forwarded headers are not trusted
to alter callback derivation.

## Consequences

The control plane can hold real Ring credentials safely, and the exact portal values are derivable
the moment a hostname exists. Two things remain manual and are intentionally not invented here: a
real public HTTPS origin, and the Ring Developer Portal configuration with its three
issued-once credentials. Operationally, losing the master key renders stored credentials
permanently unreadable — the intended failure mode, and a reason key custody belongs with the
deployment's secret manager. PostgreSQL-backed vault tests exist and run wherever a database is
reachable; they are never simulated with SQLite, because the rotation guarantees depend on
PostgreSQL row locking.

Next gate: Ring Developer Portal configuration and a single test-account link, then real WHEP
qualification against one camera.
