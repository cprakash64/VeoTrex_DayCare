# ADR 0017: Staging control-plane deployment

- Status: Accepted (R5A-R4B)
- Date: 2026-09-12

## Context

Ring requires three publicly reachable HTTPS endpoints: an Account Link URL, a Token Exchange
URL and a Webhook URL. R5A-R4A established that the repository had **no** deployment
infrastructure of any kind, so none of them could exist. This ADR records the architecture that
closes that gap without compromising the edge boundary.

## Decision

### The Jetson is never the public control plane

Ring callbacks terminate on a cloud control plane. The Jetson keeps camera transport, WebRTC
media and inference on the private LAN with no inbound exposure and no forwarded port. Putting a
device that sits on a childcare network, holding decoded video, behind an inbound public port to
satisfy a callback requirement would be the wrong trade at any convenience.

### One canonical origin spanning two applications

Ring's portal configures URLs under a single host, but the four routes live in two applications:
`/integrations/ring/link` and `/app/integrations/ring/devices` are Next.js pages, while
`/v1/integrations/ring/token-exchange` and `/v1/providers/ring/webhooks` are FastAPI. A reverse
proxy therefore routes `/v1/*` to the API and everything else to the web application. Caddy was
chosen over nginx and Traefik for one reason: automatic certificate lifecycle with the least
configuration. Only one proxy layer exists.

### Callback URLs never come from request headers

`VEOTREX_PUBLIC_ORIGIN` is set server-side and is the sole authority for callback derivation.
The proxy additionally overwrites `X-Forwarded-Host`/`X-Forwarded-Proto` and strips `Forwarded`,
`X-Original-URL` and `X-Rewrite-URL`, so no inbound value survives to the upstream. A forged
header must never be able to relocate Ring's callbacks to an attacker's host.

### Secrets reach the application by reference, not by value

`FileSecretResolver` was added so the vault master key and the two Ring secrets resolve from
orchestrator secret mounts (`file:/run/secrets/...`) rather than the process environment, where
`docker inspect` and `/proc/<pid>/environ` would expose them. `EnvironmentSecretResolver` is
unchanged and still serves development; a dispatching resolver selects by scheme.

One asymmetry is deliberate and recorded rather than hidden: `database_url` is read directly as
a setting and has no file-reference support, so it comes from a root-owned `0600` env file. The
highest-value secrets get the stronger mechanism; extending file references to the database URL
is a reasonable follow-up.

### Ring secret slots exist before Ring credentials do

Compose requires a secret's file to exist, and `FileSecretResolver` rejects an empty value. The
Ring slots are therefore created as empty files: the deployment starts, and readiness honestly
reports the secrets as missing until the portal gate fills them. Absence is reported, never
faked.

### Staging storage is durable; test storage is disposable

Staging PostgreSQL uses a persistent named volume, restart policy and healthcheck, and has **no
published port** - it is reachable only from the API on an `internal: true` network. This is
deliberately the opposite of the `postgres-test` cluster from R5A-R3-V1-R1, whose tmpfs storage
and cluster isolation exist to make destructive tests safe. The two must never be conflated.

### The migration is a discrete step

`alembic upgrade head` runs as a one-shot job behind a Compose profile, so ordinary `up` never
triggers it and API replicas cannot race each other. The job uses the same image as the API, so
the schema always matches the code being deployed.

### The control plane is CPU-only

No CUDA, TensorRT, GStreamer, model artefacts or camera stack enters these images. Those belong
to the edge. `.dockerignore` also excludes `._*`, because this checkout carries thousands of
AppleDouble sidecars whose NUL bytes break Alembic's `*.py` version discovery.

## Consequences

The staging control plane can be deployed onto any container-capable host with a public
hostname, and its security properties are enforced by static tests rather than convention: only
the proxy publishes ports, the database has no published port, secrets are references, and the
migration cannot race.

Two things remain operator decisions and are intentionally not made here: the hosting target and
the hostname. Until both exist, no certificate can be issued and no Ring portal value can be
finalised - every URL below is a function of a hostname this repository refuses to invent.

Next gate: Ring private-app creation and one test-account link, once the staging origin is live.
