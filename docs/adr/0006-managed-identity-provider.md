# ADR 0006: Managed identity provider

## Context

VeoTrex needs secure user authentication, sessions, organization context, MFA policy, signing-key
rotation, and incident response without becoming a password custodian. This is a childcare safety
system; a bespoke password stack would add critical security code unrelated to its safety mission.

Provider concepts also cannot become the tenant or authorization model. Future account linking
needs a stable internal Actor and Tenant even if the identity provider changes.

## Decision

Auth0 is the first external identity provider. The web application uses the official Auth0 Next.js
SDK v4. The API independently verifies Auth0 API access tokens and requires signed organization
context. Provider-neutral binding records map exact issuer, organization identifier, and OIDC
subject to internal Tenant and Actor records. Internal roles and PostgreSQL RLS remain authoritative.

VeoTrex will not implement password registration, verification, reset, or credential storage.
Auth0 Organization is not VeoTrex Tenant; it is external context requiring an administrative map.

## Alternatives considered

- Self-built authentication was rejected because credential storage, recovery, MFA, abuse controls,
  session revocation, and protocol maintenance create unacceptable risk.
- Self-hosted Keycloak or another OIDC provider adds control and portability but transfers patching,
  availability, key management, backups, and identity operations to VeoTrex.
- AWS Cognito is managed and integrates well in AWS, but its organization-oriented B2B model and
  current Next.js developer path are less direct for this initial deployment.
- Other managed providers can satisfy OIDC requirements and remain viable if product, regional,
  contractual, or cost needs outweigh Auth0's initial SDK and B2B fit.

## Consequences

VeoTrex depends on Auth0 availability, pricing, operational configuration, and SDK security updates.
Production requires disciplined Auth0 configuration, MFA, callback/logout allowlists, monitoring,
and a session-revocation runbook.

Migration remains feasible because services consume `AuthenticatedPrincipal`, not Auth0 claim
dictionaries, and persistence uses provider/issuer/external-ID bindings. A future OIDC provider
needs a verifier plus controlled binding migration; Tenant, Actor, permissions, audit history, and
RLS do not need redesign.
