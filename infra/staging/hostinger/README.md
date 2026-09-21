# Hostinger staging deployment runbook

Deployment of the VeoTrex control plane onto the **existing, production-active** Hostinger VPS.

## DO NOT START CADDY

The generic stack in `infra/staging/compose.yaml` includes a Caddy `proxy` service. It was
designed for an empty host. **It must never run here.**

`nginx 1.24.0` already owns `:80` and `:443` on this server and fronts five live domains
(`spendwize.co`, `thecadviewer.com`, `xpertapply.com` + `api.`, `fixcad.thelunai.com`). Starting
Caddy would fail to bind - or worse, bind first after a restart - and take those sites offline.

Use **`infra/staging/hostinger/compose.yaml`**, which contains no proxy service at all. Host
nginx proxies to the loopback ports below.

## Host facts

| | |
|---|---|
| Host | `178.16.143.10` (`srv738314.hstgr.cloud`), Ubuntu 24.04.2 LTS |
| Hostname | `daycare.veotrex.com` |
| Public origin | `https://daycare.veotrex.com` |
| Ingress | existing host nginx - **must remain** |
| Web | `127.0.0.1:3100` |
| API | `127.0.0.1:8100` |
| VeoTrex PostgreSQL | Docker-internal only, **no host port** |
| Host PostgreSQL 16 | `127.0.0.1:5432` - **do not touch** |
| Firewall | UFW active, inbound deny except 22/80/443 |

Loopback binding is the real boundary, not UFW: Docker's published-port DNAT rules bypass UFW's
INPUT chain, so `0.0.0.0` here would be publicly reachable despite `default deny incoming`.

## 1. Host preparation (operator, as root)

```bash
install -d -m 0700 /etc/veotrex-daycare/secrets
umask 077
python3 - <<'PY'
import os, secrets, stat
d = "/etc/veotrex-daycare/secrets"
pw = secrets.token_urlsafe(32)
values = {
    "postgres_password": pw,
    # The DSN is stored as its own secret so the application never receives it through the
    # process environment. postgres is the compose service name on the private network.
    "database_url": f"postgresql+psycopg://veotrex:{pw}@postgres:5432/veotrex",
    "vault_master_key": secrets.token_hex(32),   # 32 bytes for AES-256-GCM, independent value
}
for name, value in values.items():
    p = os.path.join(d, name)
    if os.path.lexists(p):
        print(f"{p} exists; leaving untouched"); continue
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    print(f"{p} mode={stat.filemode(os.stat(p).st_mode)} bytes={os.path.getsize(p)}")
# Ring slots start EMPTY: compose needs the files to exist; the resolver then reports the
# secrets as missing, so readiness is honest instead of the deployment failing.
for name in ("ring_client_secret", "ring_hmac_signing_key"):
    p = os.path.join(d, name)
    if not os.path.lexists(p):
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    print(f"{p} mode={stat.filemode(os.stat(p).st_mode)} bytes={os.path.getsize(p)}")
PY
```

Then create `/etc/veotrex-daycare/web.env` (root-owned, `0600`) with the Auth0 values:
`AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_SECRET`, `AUTH0_AUDIENCE`.
No value from this repository belongs in it.

Copy `hostinger.env.example` to `/etc/veotrex-daycare/deploy.env` and fill it in.

## 2. Deployment order

```bash
cd /srv/veotrex-daycare/infra/staging/hostinger
export ENVFILE=/etc/veotrex-daycare/deploy.env

docker compose --env-file "$ENVFILE" up -d --wait postgres          # 1. database
docker compose --env-file "$ENVFILE" --profile migrate run --rm migrate   # 2. migrate once
docker compose --env-file "$ENVFILE" up -d --wait api web           # 3. application

curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3100/    # 4. verify loopback
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/health/live
```

The migration is a one-shot job behind a profile, so an ordinary `up` never triggers it and API
replicas cannot race `alembic upgrade head`.

## 2a. Runtime database role (V1-00A)

The API must connect as `veotrex_api`, a `NOSUPERUSER NOBYPASSRLS` role, never as the
bootstrap superuser in `database_url`. Superusers bypass Row Level Security, so without this
step tenant isolation is not enforced by PostgreSQL. The API refuses to start, and readiness
reports `privileged_database_role`, when connected as a privileged role.

Two more secret files, owned by uid 10001 (the API's user inside the container) like the
existing API secrets. All lines below stay under 78 characters; run them as root.

```bash
umask 077
python3 - <<'PY'
import os, secrets, stat
d = "/etc/veotrex-daycare/secrets"
ref = os.stat(os.path.join(d, "vault_master_key"))   # API-readable exemplar
pw_path = os.path.join(d, "api_database_password")
url_path = os.path.join(d, "api_database_url")
if not os.path.lexists(pw_path):
    fd = os.open(pw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
with open(pw_path) as f:
    pw = f.read().strip()
fd = os.open(url_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"postgresql+psycopg://veotrex_api:{pw}@postgres:5432/veotrex")
for p in (pw_path, url_path):
    os.chown(p, ref.st_uid, ref.st_gid)
    os.chmod(p, 0o600)
    st = os.stat(p)
    print(p, stat.filemode(st.st_mode), st.st_uid, st.st_gid, st.st_size)
PY
```

Then provision the role (idempotent; safe to re-run after every migration):

```bash
cd /srv/veotrex-daycare/repo/infra/staging/hostinger
export ENVFILE=/etc/veotrex-daycare/deploy.env
docker compose --env-file "$ENVFILE" --profile runtime-role \
  run --rm runtime-role
```

Expected: `applied N statements for role veotrex_api` and no `PROBLEM` line. Verify from
both sides before switching the API over:

```bash
docker compose --env-file "$ENVFILE" --profile runtime-role run --rm \
  runtime-role veotrex-db-runtime-role verify --role veotrex_api
docker compose --env-file "$ENVFILE" run --rm --no-deps \
  -e VEOTREX_DATABASE_URL_REF=file:/run/secrets/api_database_url \
  api veotrex-db-runtime-role probe --migration-role veotrex
```

The first prints `verified`; the second prints only `PASS` lines. The compose file already
points the `api` service at `api_database_url`, so the switch is:

```bash
docker compose --env-file "$ENVFILE" up -d --wait api
curl -s http://127.0.0.1:8100/health/ready
```

Readiness must return `"status":"ready"` with no `reason`. The deploy order in section 2 is
therefore: `postgres` -> `migrate` -> `runtime-role` -> `api web`.

### Rollback

Prepare before switching: record the running image id and the deployed commit
(`docker compose --env-file "$ENVFILE" images api`;
`git -C /srv/veotrex-daycare/repo rev-parse HEAD`). The new build refuses to start as the
bootstrap superuser by design, so "point the API back at `database_url`" is not a rollback.
Roll the code back instead, which restores the previous DSN wiring with it:

```bash
cd /srv/veotrex-daycare/repo && git checkout <previous commit>
cd infra/staging/hostinger
docker compose --env-file "$ENVFILE" up -d --build --wait api
curl -s http://127.0.0.1:8100/health/ready
```

Leave the `veotrex_api` role and its two secret files in place: an unused locked-down role is
harmless, and dropping objects during an incident is not. PostgreSQL is never restarted.

## 3. nginx integration (never touch existing sites)

```bash
cp /etc/nginx/sites-available/zz-veotrex-daycare{,.bak-$(date +%F)} 2>/dev/null || true
cp nginx/daycare-http.conf /etc/nginx/sites-available/zz-veotrex-daycare
ln -sfn /etc/nginx/sites-available/zz-veotrex-daycare /etc/nginx/sites-enabled/zz-veotrex-daycare
nginx -t                       # MUST pass before reloading
systemctl reload nginx         # reload, never restart
```

The `zz-` prefix keeps this file last in the alphabetical `sites-enabled` glob, so it cannot
displace the existing de-facto defaults (spendwize on `:80`, thecadviewer on `:443`).

## 4. DNS, then certificate

```bash
# Add at Hostinger hPanel:  A  daycare  ->  178.16.143.10   (TTL 300)
dig +short A daycare.veotrex.com          # must return 178.16.143.10 before continuing
curl -sSI http://daycare.veotrex.com/     # must reach VeoTrex, not spendwize

certbot --nginx -d daycare.veotrex.com    # nginx authenticator, as 4 of 5 existing certs use
cp nginx/daycare-https.conf /etc/nginx/sites-available/zz-veotrex-daycare
nginx -t && systemctl reload nginx
```

Add the DNS record only at this point. Earlier, `daycare.veotrex.com` would resolve into
spendwize's Streamlit app, because no enabled nginx block declares `default_server`.

The expired `thecadviewer.com` certificate does **not** block this: certbot keeps one renewal
config per certificate and processes them independently, which is why `fixcad` and `xpertapply`
have both renewed since thecadviewer started failing.

## 5. Backups

Automated, encrypted, and retention-bounded. The logic lives in `backup/` in this repository;
installation copies it to a root-owned path:

| Component | Path |
| --- | --- |
| Script (source of truth) | `infra/staging/hostinger/backup/veotrex-daycare-backup.sh` |
| Installed as | `/usr/local/sbin/veotrex-daycare-backup` (root:root 0700) |
| Units | `/etc/systemd/system/veotrex-daycare-backup.{service,timer}` |
| Archives | `/var/backups/veotrex-daycare/veotrex-daycare-<UTC>.dump.age` (0600) |
| Public recipient | `/etc/veotrex-daycare/backup-age-recipient.txt` |
| Schedule | twice daily, 03:00 and 15:00 UTC, `Persistent=true` |
| Retention | 28 generations (14 days at two per day) |

The script is executed from `/usr/local/sbin`, never from the checkout: `/srv/veotrex-daycare`
is owned by the unprivileged `veotrex` user, and a root unit running a file that user can edit
would be a privilege-escalation path. After changing the script in Git, reinstall it.

`pg_dump --format=custom` runs inside the `postgres` container over its local socket, so no DSN,
`-W` or `PGPASSWORD` ever appears in argv, the environment, or the journal.

### Encryption

Archives are encrypted with [age](https://age-encryption.org) to a **public recipient**. The host
can create a backup and cannot read one back: the private identity lives off-host with the
operator and is never generated on, copied to, or stored on the VPS. A host compromise already
exposes the live database - it must not also expose the backup history.

### Restoring

**A backup alone is not sufficient to recover this system.** Ring credentials are stored as
AEAD ciphertext, so a restored database is unreadable - the rows are present but every credential
is undecryptable - without the matching `vault_master_key`. Recovery requires **both**:

1. an encrypted archive from `/var/backups/veotrex-daycare`, and
2. the vault master key that was current when that archive was taken.

Back the vault master key up separately from the dumps, in a password manager. Rotating it
invalidates every archive taken before the rotation; treat rotation as a migration, not a setting.

To restore, decrypt with the off-host identity and load into a **scratch** instance first - never
into the live database:

```bash
age -d -i <operator identity> /var/backups/veotrex-daycare/<archive>.dump.age > /tmp/restore.dump
pg_restore --list /tmp/restore.dump          # verify before trusting it
```

**Test the restore before relying on it.** An untested backup is not a backup.

### Rotating the backup recipient

`backup/veotrex-backup-recipient-rotate.sh`, installed root-owned like the backup script itself.
It validates the new recipient with age, refuses to reuse the active one or any recipient passed
as a forbidden argument, keeps the superseded recipient beside the file for the record, replaces
it atomically, and runs one real backup through the qualified unit.

It deliberately does **not** retire the old archives. They remain readable only by the identity
they were encrypted to, and retiring them is a separate decision that belongs after a restore
from the new recipient has actually passed. Rotation that prunes is rotation that destroys the
only copies the old key can still open.

### Verifying a restore off-host

`backup/veotrex-db-restore-verify.sh` runs on the operator's trusted machine, never here: it
needs the private recovery identity, and the host that holds the database must never see it. It
refuses outright if it finds the marks of this host.

It decrypts into RAM, restores into a throwaway `postgres:17.6-alpine` with `--network none`, no
published port and a scratch volume, then checks migration head, public table count, RLS policy
count, RLS-enabled table count and that `encrypted_credentials` is queryable, before destroying
the container, the volume and the plaintext on every exit path. It prints counts and verdicts,
never rows.

Run it after every recipient rotation. A rotated recipient is an untested backup by definition:
until this passes, nothing has shown the new key opens anything.

### Off-host copies

`/var/backups` is on the same disk as the database (`/dev/sda1`). These archives protect against
logical loss - a bad migration, an accidental `DROP` - and **not** against disk failure or loss of
the VPS. Off-host copies are a separate control and must be verified, not assumed.

## Do not

- Start Caddy, or any container binding `:80`/`:443`.
- Publish PostgreSQL on a host port, or touch the host's own PostgreSQL 16 on `127.0.0.1:5432`.
- Run `docker compose down -v` - that destroys the database and every stored Ring credential.
- Modify any existing nginx site, or add `default_server` (tracked separately as host debt).
- Add `veotrex` to the `docker` group: that group is root-equivalent on this host.
