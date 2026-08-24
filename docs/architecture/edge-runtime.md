# Edge runtime

The normal edge-agent lifecycle validates typed configuration, emits structured startup/shutdown logs
with its node ID and version, and exits gracefully on SIGINT/SIGTERM. It opens no camera, media, or
cloud connection. Stage 1D-A adds a separately invoked Ring RTSPS qualification harness; it is not
imported or activated by the normal agent or FastAPI lifecycle. Its narrow backend measures transport,
decode, reconnect, concurrency, and host resources without retaining media or running inference.

Future edge responsibility is bounded to provider connectivity, media normalization, inference,
short-lived buffering, health telemetry, and authenticated execution of assigned configuration.
Cloud responsibility is tenancy, authorization, inventory, policy catalog/evaluation, audit, durable
metadata, and fleet coordination. Safety behavior under network partition must be explicit: bounded
local operation, durable queues, idempotent upload, monotonic sequence identifiers, and reconciliation.

Jetson Orin Nano is the first target, but acceleration must be selected from advertised capabilities.
Production DeepStream integration, packaging, device enrollment, over-the-air updates, watchdogs, secure boot,
certificate rotation, signed configuration, disk-pressure policy, offline duration, and telemetry
protocol are deliberately unresolved Stage 1+ design work.
