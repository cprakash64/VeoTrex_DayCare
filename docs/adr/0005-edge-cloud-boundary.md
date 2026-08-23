# ADR 0005: Edge/cloud responsibility boundary

## Context

Media processing needs low latency and must tolerate site connectivity loss. Tenant administration,
policy lifecycle, and durable audit need centralized governance. Hardware will vary.

## Decision

Keep camera connectivity and future inference/buffering at a capability-advertising edge node. Keep
tenant identity, inventory, authorization, policy catalog/evaluation, audit, and fleet intent in the
cloud control plane. Edge configuration will be authenticated, versioned, and integrity protected.

## Alternatives considered

Cloud-only media processing increases bandwidth, privacy exposure, and outage sensitivity. Edge-only
administration fragments policy and audit control. Jetson-specific business logic blocks other nodes.

## Consequences

Privacy and responsiveness improve, while enrollment, offline reconciliation, secure updates, clock
quality, idempotency, and fleet observability become essential future work. Transport and deployment
platform are intentionally undecided.
