# ADR 0003: Versioned jurisdiction policy packs

## Context

Safety facts and legal/regulatory interpretations change independently. Arizona is first; other U.S.
jurisdictions and non-ratio rule families must follow without editing detection code.

## Decision

Represent policies as strictly validated, immutable, jurisdiction/version/effective-dated packs with
official sources and explicit rule tiers. Persist the exact document and content digest. Future
decisions reference the exact policy version and rule.

## Alternatives considered

Hardcoded conditionals are difficult to audit and deploy independently. A scalar children-per-staff
configuration loses Arizona's exceptional pair capacities. An unrestricted generic rules DSL is too
powerful and difficult to validate at this stage.

## Consequences

Policy changes are reviewable data and models remain jurisdiction-neutral. Pack signing, approval,
activation, evaluator semantics, source snapshots, and migration between schema versions remain open.
