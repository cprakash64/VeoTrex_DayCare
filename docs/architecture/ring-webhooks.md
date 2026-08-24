# Ring signed webhooks

## Trust boundary and acknowledgement

The public, POST-only `POST /v1/providers/ring/webhooks` route is intentionally outside Auth0. A
production deployment must expose it over HTTPS. It accepts only `application/json`, enforces both
declared and streamed body limits, and reads exact raw bytes. Before JSON parsing or any mutation it
resolves the existing managed signing-key reference and validates:

```text
X-Signature: sha256=<64 lower-case hex characters>
HMAC-SHA256(signing_key, exact_raw_request_body)
```

Comparison is constant-time. It does not reserialize JSON and does not reuse Stage 1B's URL-safe
Base64 nonce encoding. The body, signature, key, Account ID, provider device IDs, and credentials are
never logged. Missing/unavailable keys fail closed. External errors remain generic.

After signature validation, the explicit Ring 1.1 envelope requires bounded `meta.request_id`,
`meta.account_id`, `meta.time`, and event identity/type. Unknown 1.1 fields and event types are
accepted for forward compatibility. Other versions receive a controlled 422 and are never
reinterpreted. Clearly malformed times/shapes are rejected, but no narrow freshness window rejects
legitimate delayed retries.

The acknowledgement transaction calls a fixed-search-path `SECURITY DEFINER` ingress function. Its
narrow global inbox stores only normalized safe fields—not raw JSON or the signature—and has a unique
`request_id`. A duplicate delivery is acknowledged 200 with `duplicate=true` and cannot create a
second logical receipt. The route does no Ring API call or tenant work before returning, preserving
Ring's five-second acknowledgement requirement.

## Processing and tenant selection

`RingWebhookService.process_one()` is the deterministic worker entry point. A deployment scheduler
will call it; Stage 1C adds no broker or Internet-accessible processor endpoint. Workers atomically
claim one ready row using `FOR UPDATE SKIP LOCKED`, maintain attempts/next-attempt/failure category,
stop after five claims, and finish as `PROCESSED`, `FAILED_RETRYABLE`, or `FAILED_PERMANENT`.

Tenant trust is strictly:

```text
valid raw-body HMAC
  -> signed meta.account_id
  -> active Ring CameraProviderConnection
  -> internal tenant UUID
  -> SET LOCAL app.tenant_id
  -> tenant-owned device/event mutation under forced RLS
```

Payloads cannot supply an internal tenant UUID. Device IDs alone never select a tenant. Unknown
Account IDs are permanently failed before tenant context is set. The inbox has no tenant and no
normal table privileges; production grants only the four ingress/claim/finish/resolve function
signatures to its least-privileged runtime role. Tenant devices, components, and provider events
carry composite tenant-safe foreign keys and forced RLS.

## Supported events

The detailed Ring Notifications/API reference reviewed 2026-08-23 is authoritative over older pages
with shorter lists. Stage 1C handles:

- `motion_detected` and `button_press`: normalized provider telemetry only—not a person, child,
  teacher, intruder, or VeoTrex safety incident.
- `device_added`: durable acknowledgement first, then bounded detail fetch and idempotent reconcile;
  no room assignment.
- `device_removed`: device/components become removed and Cameras disabled; history remains.
- `device_online` / `device_offline`: Ring-only status with provider-time ordering.
- `app_integration_added`: telemetry confirmation only; it cannot bypass Stage 1B activation.
- `app_integration_removed`: lock and transition the exact connection to `DISCONNECTED`,
  `REMOTE_REMOVED`, disable all devices/Cameras, delete the context-bound vault credential, clear the
  reference, and create a system audit event. Row locking serializes this with Stage 1B refresh, so a
  refresh started after removal cannot use the credential; deletion is idempotent.
- `subscription_activated` / `subscription_deactivated`: telemetry only; no billing, tenant deletion,
  or invented access semantics.

Every valid 1.1 event, including an unknown future type, becomes a minimized `ProviderEvent` after
tenant resolution. Unknown types cause no lifecycle state mutation and should emit the documented
low-cardinality unsupported-event operational signal. Component IDs are preserved as opaque strings;
an unknown/new component does not crash processing. A later device sync establishes its identity.

Provider telemetry retention must be configured before production volume; it is not permanent
childcare evidence. Recommended metrics are `ring_webhooks_received_total`,
`ring_webhook_invalid_signature_total`, `ring_webhook_duplicate_total`, and
`ring_webhook_processing_failures_total`, labelled only by bounded outcome/event taxonomy.

## Recovery and replay model

- Valid signature plus duplicate `request_id`: acknowledge, never reprocess.
- Unknown signed Account ID: permanent failure, no tenant selected.
- Transient device detail/provider/database failure: bounded retry with next-attempt time.
- Poisoned event: permanent after the attempt cap; no infinite loop.
- Removal racing manual sync: both lock and recheck the connection; `REMOTE_REMOVED` cannot be
  resurrected by the sync write phase.
- Integration removal racing token refresh: the same connection row lock serializes the operations;
  removal makes future credential acquisition fail and deletes the final vaulted generation.

Operational logs may contain operation, known event taxonomy, internal connection UUID, HTTP status,
latency, safe failure category, and correlation ID only. HTTPS termination, worker scheduling,
telemetry retention, alert thresholds, least-privilege function grants, and the production managed
vault remain deployment prerequisites.
