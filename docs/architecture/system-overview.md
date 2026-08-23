# System overview

## Stage 0 scope

VeoTrex is a multi-tenant childcare safety platform foundation. The control plane stores tenant,
facility topology, camera inventory references, edge inventory, policy versions, identities, and
audit events. The edge process currently has lifecycle and configuration behavior only. The web
application is a deliberately minimal operational shell.

```text
Web shell -> API/control plane -> PostgreSQL
                    |
                    +-> versioned policy catalog
                    |
                    +-> future signed edge configuration -> Edge agent -> future provider adapter
```

Detection will eventually produce facts. A separately reviewed policy evaluator will interpret
facts under an immutable policy version. Neither detector code nor provider adapters contain
jurisdiction rules.

## Reliability boundaries

The API liveness endpoint proves the process can respond. Readiness executes `SELECT 1` against
PostgreSQL. Database loss makes readiness fail without claiming the process is dead. Structured
JSON logs identify service, environment, version, and HTTP request ID. UTC, timezone-aware database
timestamps are mandatory; facilities separately record their IANA timezone for presentation and
policy-effective-time questions.

PostgreSQL is the Stage 0 system of record for metadata only. Raw video, images, audio, credentials,
and model payloads do not belong in it. Media storage and its independently configurable retention
boundary remain a later decision.

## Deployment shape

Local development uses one PostgreSQL container and host-run services. Production topology,
identity provider, secret manager, object/evidence storage, and deployment platform are open. No
message broker, cache, orchestrator, or search engine is justified in Stage 0.

## Open questions

- Cloud region, availability objective, recovery point/time objectives, and backup restore tests
- Identity provider and tenant-aware authorization service
- Policy pack signing, approval quorum, rollout, rollback, and distribution protocol
- Offline edge buffering, command acknowledgement, and configuration reconciliation semantics
- Evidence storage format, immutability mechanism, chain of custody, and legal hold
