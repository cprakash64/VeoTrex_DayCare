#!/bin/bash
# VeoTrex Childcare staging database backup: dump, encrypt, publish atomically, prune.
#
# Runs as root from /usr/local/sbin, NOT from the git checkout. The checkout is owned by the
# unprivileged `veotrex` deployment user, so executing it from there as root would let that user
# escalate by editing this file. The repository is the source of truth; installation copies it to
# a root-owned path and records the checksum.
#
# The database password never appears: pg_dump runs inside the already-qualified postgres
# container over its local socket, so there is no DSN, no -W, and no PGPASSWORD anywhere.
#
# The age RECIPIENT is a public key. This host can encrypt a backup and cannot read one back;
# the private identity lives off-host with the operator. That is deliberate - a host compromise
# already exposes the live database, but it must not also expose the backup history.

set -euo pipefail

DEST=${VEOTREX_BACKUP_DEST:-/var/backups/veotrex-daycare}
RECIPIENTS=${VEOTREX_BACKUP_RECIPIENTS:-/etc/veotrex-daycare/backup-age-recipient.txt}
COMPOSE_FILE=${VEOTREX_COMPOSE_FILE:-/srv/veotrex-daycare/repo/infra/staging/hostinger/compose.yaml}
ENV_FILE=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}
RETAIN=${VEOTREX_BACKUP_RETAIN:-28}
LOCK=${VEOTREX_BACKUP_LOCK:-/var/lock/veotrex-daycare-backup.lock}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "FAILED: $*"; exit 1; }

# --- 1. bounded lock: a slow dump must never overlap the next timer firing -------------------
exec 9>"$LOCK" || fail "cannot open lock $LOCK"
if ! flock -n 9; then
    log "another backup holds the lock; exiting without touching existing backups"
    exit 75
fi

[ -r "$RECIPIENTS" ] || fail "age recipients file is unreadable: $RECIPIENTS"
[ -s "$RECIPIENTS" ] || fail "age recipients file is empty: $RECIPIENTS"
[ -d "$DEST" ] || fail "backup destination does not exist: $DEST"

umask 077
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL="$DEST/veotrex-daycare-$STAMP.dump.age"
# Never overwrite: a same-second rerun must fail loudly rather than destroy a good backup.
[ -e "$FINAL" ] && fail "backup already exists: $FINAL"

WORK=$(mktemp -d "$DEST/.work.XXXXXXXX") || fail "cannot create work directory"
# Removes any plaintext dump on EVERY exit path, including failure part-way through encryption.
trap 'rm -rf -- "$WORK"' EXIT
PLAIN="$WORK/dump"
ENC="$WORK/dump.age"

# --- 2. dump from the qualified container (no credential on argv or in the environment) ------
START=$(date +%s)
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
        pg_dump -U veotrex -d veotrex --format=custom > "$PLAIN"; then
    fail "pg_dump returned non-zero; existing backups left untouched"
fi
[ -s "$PLAIN" ] || fail "pg_dump produced an empty file"
# Custom-format archives start with the PGDMP magic. A plain-SQL or truncated file is not usable
# with pg_restore and must not be published as if it were.
head -c 5 "$PLAIN" | grep -q 'PGDMP' || fail "dump is not a PostgreSQL custom-format archive"
PLAIN_BYTES=$(stat -c %s "$PLAIN")

# --- 3. encrypt to the public recipient ------------------------------------------------------
age -R "$RECIPIENTS" -o "$ENC" "$PLAIN" || fail "age encryption failed"
[ -s "$ENC" ] || fail "encrypted artefact is empty"
head -c 22 "$ENC" | grep -q 'age-encryption.org' || fail "encrypted artefact is not an age file"

# --- 4. destroy the plaintext BEFORE publishing ----------------------------------------------
shred -u -- "$PLAIN" 2>/dev/null || rm -f -- "$PLAIN"
[ -e "$PLAIN" ] && fail "plaintext dump survived removal"

# --- 5. publish atomically: readers never observe a partial archive --------------------------
sync -- "$ENC" 2>/dev/null || true
mv -- "$ENC" "$FINAL" || fail "could not publish $FINAL"
chmod 0600 -- "$FINAL"
ENC_BYTES=$(stat -c %s "$FINAL")
log "backup published $FINAL plaintext_bytes=$PLAIN_BYTES encrypted_bytes=$ENC_BYTES duration=$(( $(date +%s) - START ))s"

# --- 6. retention, ONLY after a successful publish -------------------------------------------
# Every earlier exit path returns before this point, so a failed run can never prune anything.
mapfile -t ALL < <(ls -1t -- "$DEST"/veotrex-daycare-*.dump.age 2>/dev/null || true)
if [ "${#ALL[@]}" -gt "$RETAIN" ]; then
    for old in "${ALL[@]:$RETAIN}"; do
        [ "$old" = "$FINAL" ] && continue   # the backup just written is never a pruning candidate
        rm -f -- "$old" && log "pruned $old"
    done
fi
log "retention=$RETAIN generations; $(ls -1 -- "$DEST"/veotrex-daycare-*.dump.age 2>/dev/null | wc -l) backup(s) retained"
