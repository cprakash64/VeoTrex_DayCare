# ADR 0001: Production monorepo

## Context

The API, web control plane, edge lifecycle, policy packs, migrations, and documentation must evolve
with compatible contracts while the team and product boundaries are still small.

## Decision

Use one repository with independently packaged API, web, and edge applications, shared CI, and
explicit directory ownership. Do not copy the industrial VeoTrex repository.

## Alternatives considered

Separate repositories improve independent release permissions but add cross-repository coordination
before interfaces stabilize. A single deployable would improperly couple cloud, browser, and edge.

## Consequences

Atomic contract changes and one review surface are simpler. CI can become longer, and ownership and
release automation must preserve deployable independence. Extraction remains possible after stable
interfaces emerge.
