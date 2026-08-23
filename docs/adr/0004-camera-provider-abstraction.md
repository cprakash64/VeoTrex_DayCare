# ADR 0004: Camera provider abstraction

## Context

The first facility owns Ring cameras, but VeoTrex must support ONVIF/RTSP, deterministic files,
simulations, and future vendors without contaminating domain logic with provider credentials.

## Decision

Define an async provider protocol for discovery, capabilities, managed stream lifecycle, health,
reconnect, recording retrieval, and snapshots. Store opaque provider IDs and secret references only.
Adapters own provider protocols and secret resolution.

## Alternatives considered

Calling Ring directly from business services creates lock-in and leaks provider semantics. A lowest-
common-denominator stream URL omits discovery, health, lifecycle, and recordings. A universal media
framework is premature before provider constraints are measured.

## Consequences

Provider adapters can be conformance-tested and replaced. Some capabilities remain optional. The
byte-returning media placeholders must become bounded streaming contracts before real media use.
