#!/bin/bash
# Prove an encrypted database archive can actually be restored - off-host, on the operator's
# trusted machine, using the private identity that lives only there.
#
# An untested backup is not a backup, and a backup whose recipient was just rotated is untested
# by definition: nothing has yet shown that the new key opens it. This does the whole thing for
# real - decrypt, restore into a throwaway PostgreSQL of the same version, and check the
# structure the application depends on.
#
# It prints counts and verdicts. It never prints the identity, the passphrase, the dump, or any
# row contents. The restored database is destroyed on every exit path, including interruption.
#
# Usage: veotrex-db-restore-verify.sh <archive.age> <wrapped-identity.age> <expected-sha256>

set -euo pipefail

EXPECT_HEAD=${VEOTREX_EXPECT_HEAD:-0006_vault_boundary}
EXPECT_TABLES=${VEOTREX_EXPECT_TABLES:-22}
EXPECT_POLICIES=${VEOTREX_EXPECT_POLICIES:-15}
EXPECT_RLS_TABLES=${VEOTREX_EXPECT_RLS_TABLES:-15}
IMAGE=${VEOTREX_POSTGRES_IMAGE:-postgres:17.6-alpine}
DOCKER=${VEOTREX_DOCKER:-docker}

say()  { printf '%s\n' "$*"; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
step() { printf '\n--- %s ---\n' "$*"; }

# --- portability ------------------------------------------------------------------------------
# This runs on the operator's own machine, which may be macOS. GNU coreutils are not a given
# there: sha256sum, shred and `stat -c` do not exist, and /dev/shm is Linux-only. The handful of
# tools that differ are resolved once, here, instead of being assumed and failing halfway through
# a recovery rehearsal.
if command -v sha256sum >/dev/null 2>&1; then
    digest() { sha256sum | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    digest() { shasum -a 256 | cut -d' ' -f1; }
else
    echo "FAILED: neither sha256sum nor shasum is available" >&2; exit 1
fi

file_size() { stat -c %s -- "$1" 2>/dev/null || stat -f %z -- "$1"; }

# Overwrite where the platform can, remove either way. macOS has no shred; rm -P is its analogue.
wipe() {
    [ $# -gt 0 ] || return 0
    if command -v shred >/dev/null 2>&1; then
        shred -u -- "$@" 2>/dev/null && return 0
    fi
    rm -P -f -- "$@" 2>/dev/null || rm -f -- "$@" 2>/dev/null || true
}

wipe_tree() {
    [ -d "$1" ] || return 0
    find "$1" -type f -print0 2>/dev/null | while IFS= read -r -d "" victim; do wipe "$victim"; done
}

[ $# -eq 3 ] || fail "usage: $0 <archive.age> <wrapped-identity.age> <expected-sha256>"
ARCHIVE=$1
IDENTITY_IN=$2
EXPECTED_SHA=$3

# --- 0. never on the host it protects ---------------------------------------------------------
# This needs the private identity. The machine that runs the deployment must never see it.
for marker in \
    "${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}" \
    /usr/local/sbin/veotrex-vault-key-escrow \
    /usr/local/sbin/veotrex-daycare-backup
do
    if [ -e "$marker" ]; then
        fail "$marker exists: this is the deployment host, and the identity must never reach it"
    fi
done

for tool in "$DOCKER" age; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required on this machine"
done
[ -r "$ARCHIVE" ]     || fail "archive is unreadable: $ARCHIVE"
[ -r "$IDENTITY_IN" ] || fail "wrapped identity is unreadable: $IDENTITY_IN"

umask 077
RAMDIR=""
for candidate in "${XDG_RUNTIME_DIR:-}" /dev/shm; do
    [ -n "$candidate" ] && [ -d "$candidate" ] && [ -w "$candidate" ] && { RAMDIR=$candidate; break; }
done
WORK=$(mktemp -d "${RAMDIR:-${TMPDIR:-/tmp}}/veotrex-restore.XXXXXXXX") || fail "no work directory"
chmod 0700 -- "$WORK"
SUFFIX=$(basename -- "$WORK" | tr -dc 'a-zA-Z0-9' | tail -c 8)
CONTAINER="veotrex-restore-$SUFFIX"
VOLUME="veotrex-restore-$SUFFIX"

cleanup() {
    "$DOCKER" rm -f -- "$CONTAINER" >/dev/null 2>&1 || true
    "$DOCKER" volume rm -f -- "$VOLUME" >/dev/null 2>&1 || true
    wipe_tree "$WORK"
    rm -rf -- "$WORK"
}
trap cleanup EXIT INT TERM

# --- 1. the bytes are the bytes the VPS published ---------------------------------------------
step "1  archive integrity"
ACTUAL_SHA=$(digest < "$ARCHIVE")
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] ||
    fail "archive sha256 $ACTUAL_SHA does not match the value published on the VPS"
say "ARCHIVE_SHA256=$ACTUAL_SHA  (matches)"

# --- 2. decrypt, in RAM, with the off-host identity -------------------------------------------
step "2  decrypt with the off-host recovery identity"
IDENTITY="$IDENTITY_IN"
if head -c 22 -- "$IDENTITY_IN" | grep -q 'age-encryption.org'; then
    say "the identity is passphrase-wrapped; it will be unwrapped into RAM only"
    age -d -o "$WORK/identity" -- "$IDENTITY_IN" || fail "could not unwrap the recovery identity"
    IDENTITY="$WORK/identity"
fi
LC_ALL=C grep -q 'AGE-SECRET-KEY-' -- "$IDENTITY" || fail "that file is not an age private identity"
age -d -i "$IDENTITY" -o "$WORK/dump" -- "$ARCHIVE" ||
    fail "decryption failed - this archive is NOT recoverable with this identity"
wipe "$WORK/identity"
[ -s "$WORK/dump" ] || fail "decryption produced an empty dump"
head -c 5 -- "$WORK/dump" | grep -q 'PGDMP' || fail "not a PostgreSQL custom-format archive"
say "DECRYPT=ok  format=PGDMP  bytes=$(file_size "$WORK/dump")"

# --- 3. an isolated PostgreSQL of the same version --------------------------------------------
step "3  isolated restore target"
# --network none: no route to anything, least of all the production host. No published port, a
# throwaway volume, and a password that exists for the life of this script only.
"$DOCKER" volume create -- "$VOLUME" >/dev/null || fail "could not create the scratch volume"
"$DOCKER" run -d --name "$CONTAINER" --network none \
    -v "$VOLUME":/var/lib/postgresql/data \
    -e POSTGRES_PASSWORD="$(head -c 18 /dev/urandom | base64)" \
    -- "$IMAGE" >/dev/null || fail "could not start $IMAGE"
READY=no
for _ in $(seq 1 60); do
    if "$DOCKER" exec -- "$CONTAINER" pg_isready -U postgres -q 2>/dev/null; then READY=yes; break; fi
    sleep 1
done
[ "$READY" = yes ] || fail "the scratch PostgreSQL never became ready"
say "CONTAINER=$CONTAINER  VOLUME=$VOLUME  network=none  published_ports=none"

psql_() { "$DOCKER" exec -- "$CONTAINER" psql -U postgres -d veotrex_restore -Atqc "$1"; }

# --- 4. the archive describes what it should --------------------------------------------------
step "4  archive table of contents"
"$DOCKER" cp -- "$WORK/dump" "$CONTAINER:/tmp/dump" || fail "could not stage the dump"
TOC="$WORK/toc"
"$DOCKER" exec -- "$CONTAINER" pg_restore --list /tmp/dump > "$TOC" ||
    fail "pg_restore --list rejected the archive"
grep -q 'alembic_version' -- "$TOC" || fail "no alembic_version in the archive"
grep -q 'encrypted_credentials' -- "$TOC" || fail "no encrypted_credentials in the archive"
TOC_TABLE_DATA=$(grep -c 'TABLE DATA' -- "$TOC" || true)
say "TOC_TABLE_DATA_SECTIONS=$TOC_TABLE_DATA"

# --- 5. restore for real ----------------------------------------------------------------------
step "5  restore"
"$DOCKER" exec -- "$CONTAINER" psql -U postgres -qc 'CREATE ROLE veotrex LOGIN' >/dev/null ||
    fail "could not create the owning role"
"$DOCKER" exec -- "$CONTAINER" createdb -U postgres -O veotrex veotrex_restore ||
    fail "could not create the scratch database"
RESTORE_STATUS=0
"$DOCKER" exec -- "$CONTAINER" pg_restore -U postgres -d veotrex_restore /tmp/dump \
    > "$WORK/restore.out" 2> "$WORK/restore.err" || RESTORE_STATUS=$?
ERROR_LINES=$(grep -c 'pg_restore: error' -- "$WORK/restore.err" || true)
WARNING_LINES=$(grep -c 'pg_restore: warning' -- "$WORK/restore.err" || true)
say "PG_RESTORE_EXIT=$RESTORE_STATUS"
say "RESTORE_ERROR_LINES=$ERROR_LINES"
say "RESTORE_WARNING_LINES=$WARNING_LINES"
[ "$RESTORE_STATUS" -eq 0 ] || fail "pg_restore exited $RESTORE_STATUS"
[ "$ERROR_LINES" -eq 0 ] || fail "pg_restore reported $ERROR_LINES error line(s)"

# --- 6. the structure the application depends on ----------------------------------------------
step "6  structural verification"
HEAD=$(psql_ 'SELECT version_num FROM alembic_version')
TABLES=$(psql_ "SELECT count(*) FROM information_schema.tables
                WHERE table_schema='public' AND table_type='BASE TABLE'")
POLICIES=$(psql_ "SELECT count(*) FROM pg_policies WHERE schemaname='public'")
RLS_TABLES=$(psql_ "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relrowsecurity")
CREDENTIALS=$(psql_ 'SELECT count(*) FROM encrypted_credentials') ||
    fail "encrypted_credentials is not queryable"

say "MIGRATION_HEAD=$HEAD  (expected $EXPECT_HEAD)"
say "PUBLIC_TABLES=$TABLES  (expected $EXPECT_TABLES)"
say "RLS_POLICIES=$POLICIES  (expected $EXPECT_POLICIES)"
say "RLS_ENABLED_TABLES=$RLS_TABLES  (expected $EXPECT_RLS_TABLES)"
say "ENCRYPTED_CREDENTIALS_ROWS=$CREDENTIALS  (queryable)"
[ "$HEAD" = "$EXPECT_HEAD" ]             || fail "migration head mismatch"
[ "$TABLES" = "$EXPECT_TABLES" ]         || fail "public table count mismatch"
[ "$POLICIES" = "$EXPECT_POLICIES" ]     || fail "RLS policy count mismatch"
[ "$RLS_TABLES" = "$EXPECT_RLS_TABLES" ] || fail "RLS-enabled table count mismatch"

# Representative counts only - how many rows, never what is in them.
for table in tenants facilities cameras actors role_assignments audit_events; do
    say "ROWS_$table=$(psql_ "SELECT count(*) FROM $table")"
done

step "verdict"
say "DB_RESTORE_VERIFY=QUALIFIED"
say "ARCHIVE=$(basename -- "$ARCHIVE")"
say "ARCHIVE_SHA256=$ACTUAL_SHA"
say "The scratch container, volume and decrypted dump are destroyed on exit."
