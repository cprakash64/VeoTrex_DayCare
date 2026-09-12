# Staging control-plane deployment runbook

The public staging control plane that Ring calls. The Jetson is **not** part of this
deployment: it stays on the private LAN with no inbound exposure, and Ring callbacks
terminate here.

```
Internet ──HTTPS 443──▶ proxy (Caddy, automatic TLS)
                          ├── /v1/*  ──▶ api  (FastAPI)   ──▶ postgres (internal only)
                          └── /*     ──▶ web  (Next.js)
```

Only `proxy` publishes ports. `web`, `api` and `postgres` are unreachable from the Internet.

## Prerequisites (operator-supplied)

- A container-capable Linux host in a US region, with Docker Engine and Compose v2.
- An approved fully qualified staging hostname with DNS A/AAAA (or CNAME) already pointing
  at the host. **This repository does not choose or invent a hostname.**
- Inbound 80 and 443 reachable (80 is required for the ACME HTTP-01 challenge).
- Auth0 application configured for the staging origin.

## 1. Secret bootstrap (run on the deployment host, as root)

Secrets are generated **on the host**. They are never printed, never committed, never pasted
into a chat tool, and never passed as command-line arguments.

```bash
install -d -m 0700 /etc/veotrex/staging/secrets
umask 077
python3 - <<'PY'
import os, secrets, stat
d = "/etc/veotrex/staging/secrets"
# Independent values. The vault master key is NOT derived from, or equal to, any other secret.
for name, generator in (
    ("postgres_password", lambda: secrets.token_urlsafe(32)),
    ("vault_master_key", lambda: secrets.token_hex(32)),   # 32 bytes for AES-256-GCM
):
    p = os.path.join(d, name)
    if os.path.lexists(p):
        print(f"{p} exists; leaving untouched")
        continue
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(generator())
    print(f"{p} mode={stat.filemode(os.stat(p).st_mode)} bytes={os.path.getsize(p)}")

# Ring slots start EMPTY: compose needs the files to exist, and the application reports the
# secrets as missing until the Ring portal gate fills them.
for name in ("ring_client_secret", "ring_hmac_signing_key"):
    p = os.path.join(d, name)
    if not os.path.lexists(p):
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    print(f"{p} mode={stat.filemode(os.stat(p).st_mode)} bytes={os.path.getsize(p)}")
PY
```

Then create the two root-owned env files, `0600`, containing **no** values from this repository:

- `/etc/veotrex/staging/api.env` — `VEOTREX_DATABASE_URL=postgresql+psycopg://veotrex:<the
  generated postgres_password>@postgres:5432/veotrex`
  (`database_url` is read directly as a setting and has no file-reference support, unlike the
  vault and Ring keys.)
- `/etc/veotrex/staging/web.env` — `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`,
  `AUTH0_SECRET`, `AUTH0_AUDIENCE`.

Finally copy `staging.env.example` to `/etc/veotrex/staging/deploy.env`, fill in the hostname
and ACME email, and keep it outside the repository.

## 2. Deployment order

Never let API replicas race the migration. Run these steps in order:

```bash
cd infra/staging
export ENVFILE=/etc/veotrex/staging/deploy.env

# 1. database only
docker compose --env-file "$ENVFILE" up -d --wait postgres

# 2. migrate exactly once (one-shot job, behind the "migrate" profile)
docker compose --env-file "$ENVFILE" --profile migrate run --rm migrate

# 3. verify head
docker compose --env-file "$ENVFILE" exec -T postgres \
  psql -U veotrex -d veotrex -c "SELECT version_num FROM alembic_version"

# 4. application, then the public proxy
docker compose --env-file "$ENVFILE" up -d --wait api web
docker compose --env-file "$ENVFILE" up -d --wait proxy
```

Certificate issuance happens automatically on first request once DNS resolves.

## 3. Verification

```bash
# From OUTSIDE the host:
curl -sSI https://<host>/integrations/ring/link          # web route, 200/302
curl -sS  -o /dev/null -w '%{http_code}\n' -X POST https://<host>/v1/providers/ring/webhooks
                                                          # API route, rejects unsigned
curl -sSI http://<host>/                                  # redirects to HTTPS
```

The database must **not** be reachable: `nc -z <host> 5432` must fail.

## 4. Backups

Staging holds encrypted Ring credentials once the portal gate completes, so it needs a
recovery story before real credentials are introduced.

```bash
# Nightly logical backup, encrypted at rest, retained 7 days.
docker compose --env-file "$ENVFILE" exec -T postgres \
  pg_dump -U veotrex -d veotrex --format=custom | \
  age -r "$BACKUP_RECIPIENT" > "/var/backups/veotrex/staging-$(date +%F).dump.age"
```

Restore is the inverse (`age -d` then `pg_restore`). **Test the restore before relying on it** —
an untested backup is not a backup. Note that a restored database is useless without the vault
master key: back up that key separately, in your password manager or secret store, never
alongside the database dump.

## 5. Restart

```bash
docker compose --env-file "$ENVFILE" restart api web proxy
```

The named volume `veotrex-staging-postgres` survives restarts. Never run `down -v` against
staging: that destroys the database and, with it, every stored Ring credential.

## Notes

- `._*` AppleDouble files are excluded from build contexts by `.dockerignore`. They are binary
  and break Alembic's `*.py` migration discovery; deployments build from a clean checkout.
- The `migrate` job and the API image are the same image, so the migration always matches the
  code being deployed.
