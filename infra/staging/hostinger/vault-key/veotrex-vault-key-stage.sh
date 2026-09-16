#!/bin/bash
# One bounded custody run, executed as root from a root-owned path.
#
# Six phases, each a precondition for the next: install the qualified gate, verify the
# installation, baseline the running deployment, install and validate the public recovery
# recipient, escrow the live vault master key, report non-secret metadata. Any failure aborts
# before the next phase; nothing after a failed phase runs.
#
# Safe to rerun. If an escrow artefact already exists it is REPORTED, never replaced: a second
# run must not produce a second ciphertext of the same key, and must never overwrite the first.
#
# This script never reads the vault master key. It stats it before and after, and the gate it
# calls encrypts the file in place. The key is not printed, copied, rotated or varied.
#
# Argument: the dedicated vault-recovery PUBLIC age recipient (age1...). Public material only -
# safe on argv. A private identity must never reach this host.

set -euo pipefail

GATE_SHA256=b315c9d4d49e6d0c1f3591929fd1f7dc06a5900f2234b4d7b0ddf7f3b791cc37
REPO=${VEOTREX_REPO:-/srv/veotrex-daycare/repo}
SRC="$REPO/infra/staging/hostinger/vault-key/veotrex-vault-key-escrow.sh"
GATE=${VEOTREX_GATE:-/usr/local/sbin/veotrex-vault-key-escrow}
ENVF=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}
COMPOSE=${VEOTREX_COMPOSE_FILE:-$REPO/infra/staging/hostinger/compose.yaml}
REC=${VEOTREX_VAULT_RECOVERY_RECIPIENT:-/etc/veotrex-daycare/vault-recovery-recipient.txt}
BAK=${VEOTREX_BACKUP_RECIPIENTS:-/etc/veotrex-daycare/backup-age-recipient.txt}
OUT=${VEOTREX_VAULT_ESCROW_DIR:-/root/veotrex-vault-escrow}
ORIGIN=${VEOTREX_ORIGIN_URL:-https://daycare.veotrex.com}
ARCHIVES=${VEOTREX_BACKUP_DEST:-/var/backups/veotrex-daycare}
DEPLOY_USER=${VEOTREX_DEPLOY_USER:-veotrex}
TIMER=veotrex-daycare-backup.timer

say()  { printf '%s\n' "$*"; }
fail() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }
phase() { printf '\n--- %s ---\n' "$*"; }

NEW_RECIPIENT=${1:-}
[ -n "$NEW_RECIPIENT" ] || fail "usage: $0 <vault-recovery age1 recipient>"
[ "$(id -u)" -eq 0 ] || fail "must run as root"
case "$NEW_RECIPIENT" in
    AGE-SECRET-KEY-*) fail "that is a PRIVATE identity; only the age1 recipient belongs here" ;;
    age1*) : ;;
    *) fail "not an age1 recipient: $NEW_RECIPIENT" ;;
esac

umask 077

# The live key is only ever stat'ed. Recorded now, compared again after the escrow.
SECRETS_DIR=$(sed -n 's/^[[:space:]]*VEOTREX_HOSTINGER_SECRETS_DIR=//p' "$ENVF" | tail -1 | tr -d '"\r')
[ -n "$SECRETS_DIR" ] || fail "VEOTREX_HOSTINGER_SECRETS_DIR is not set in $ENVF"
KEY="$SECRETS_DIR/vault_master_key"
[ -f "$KEY" ] || fail "vault master key not found: $KEY"
KEY_BEFORE=$(stat -c '%u:%g %a %s %Y' -- "$KEY")

# ------------------------------------------------------------------ PHASE 1: install the gate
phase "PHASE 1  install the qualified escrow gate"
[ -f "$SRC" ] || fail "gate source not found: $SRC"
SRC_SHA=$(sha256sum < "$SRC" | cut -d' ' -f1)
[ "$SRC_SHA" = "$GATE_SHA256" ] || fail "gate source checksum $SRC_SHA != qualified $GATE_SHA256"
install -o root -g root -m 0700 -- "$SRC" "$GATE.new"
mv -f -- "$GATE.new" "$GATE"          # atomic replacement; no window with a partial file
say "installed $GATE"

# --------------------------------------------------------- PHASE 2: verify the installed copy
phase "PHASE 2  verify the installation"
INST_SHA=$(sha256sum < "$GATE" | cut -d' ' -f1)
[ "$INST_SHA" = "$GATE_SHA256" ] || fail "installed checksum $INST_SHA != qualified"
OWNMODE=$(stat -c '%U:%G %a' -- "$GATE")
[ "$OWNMODE" = "root:root 700" ] || fail "installed as $OWNMODE, expected root:root 700"
runuser -u "$DEPLOY_USER" -- test -w "$GATE" && fail "$DEPLOY_USER can write the installed gate"
runuser -u "$DEPLOY_USER" -- test -r "$GATE" && fail "$DEPLOY_USER can read the installed gate"
say "INSTALLED_ESCROW_SHA256=$INST_SHA"
say "INSTALLED_OWNER_MODE=$OWNMODE"
say "DEPLOY_USER_CAN_MODIFY=no"

# ------------------------------------------------------------- PHASE 3: deployment baseline
phase "PHASE 3  application baseline"
docker compose --env-file "$ENVF" -f "$COMPOSE" ps || fail "docker compose ps failed"
for path in "/" "/health/live" "/health/ready"; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$ORIGIN$path") ||
        fail "could not reach $ORIGIN$path"
    [ "$code" = "200" ] || fail "$ORIGIN$path returned $code"
    say "HTTP 200 $path"
done
systemctl is-enabled "$TIMER" >/dev/null || fail "$TIMER is not enabled"
systemctl is-active  "$TIMER" >/dev/null || fail "$TIMER is not active"
say "BACKUP_TIMER=enabled,active"
ARCHIVE_COUNT=$(find "$ARCHIVES" -maxdepth 1 -name '*.dump.age' | wc -l)
[ "$ARCHIVE_COUNT" -ge 1 ] || fail "no database archives in $ARCHIVES"
say "BACKUP_ARCHIVES=$ARCHIVE_COUNT"
say "DB_BACKUP_RECIPIENT=$(cat -- "$BAK")"

# ------------------------------------------------- PHASE 4: recovery recipient, public only
phase "PHASE 4  vault-recovery recipient"
printf '' | age -r "$NEW_RECIPIENT" -o /dev/null 2>/dev/null ||
    fail "age rejects this recipient: $NEW_RECIPIENT"
[ -f "$BAK" ] && [ -r "$BAK" ] && [ -s "$BAK" ] || fail "database-backup recipient unusable: $BAK"
grep -qxF "$NEW_RECIPIENT" -- "$BAK" &&
    fail "recovery recipient is the database-backup recipient"
printf '%s\n' "$NEW_RECIPIENT" > "$REC"
chown root:root -- "$REC"
chmod 0600 -- "$REC"
say "VAULT_RECOVERY_PUBLIC_RECIPIENT=$NEW_RECIPIENT"
say "RECIPIENT_SEPARATION=distinct from the active database-backup recipient"
PRIVATE=$(grep -rlI 'AGE-SECRET-KEY-' "$(dirname -- "$REC")" "$OUT" 2>/dev/null || true)
[ -z "$PRIVATE" ] || fail "private age material present on this host: $PRIVATE"
say "PRIVATE_AGE_MATERIAL=absent"

# -------------------------------------------------------------- PHASE 5: live vault escrow
phase "PHASE 5  live vault escrow"
EXISTING=$(find "$OUT" -maxdepth 1 -name '*.age' 2>/dev/null | wc -l)
if [ "$EXISTING" -gt 0 ]; then
    say "escrow artefact already exists; not creating a second one"
else
    "$GATE" || fail "the escrow gate refused; nothing was written"
fi

# ------------------------------------------------------------- PHASE 6: post-escrow report
phase "PHASE 6  result"
COUNT=$(find "$OUT" -maxdepth 1 -name '*.age' | wc -l)
[ "$COUNT" -eq 1 ] || fail "expected exactly one artefact in $OUT, found $COUNT"
ART=$(find "$OUT" -maxdepth 1 -name '*.age')
ART_MODE=$(stat -c '%U:%G %a' -- "$ART")
[ "$ART_MODE" = "root:root 600" ] || fail "artefact is $ART_MODE, expected root:root 600"
KEY_AFTER=$(stat -c '%u:%g %a %s %Y' -- "$KEY")
[ "$KEY_BEFORE" = "$KEY_AFTER" ] || fail "vault key metadata changed during this run"

say "ESCROW_ARTIFACT_PATH=$ART"
say "ESCROW_ARTIFACT_SIZE=$(stat -c %s -- "$ART")"
say "ESCROW_ARTIFACT_SHA256=$(sha256sum < "$ART" | cut -d' ' -f1)"
say "ESCROW_ARTIFACT_MODE=$ART_MODE"
say "VAULT_KEY_COMMITMENT=$(sed -n 's/^key_fingerprint_sha256_16=//p' -- "$ART.manifest")"
say "VAULT_KEY_FORM=$(sed -n 's/^key_form=//p' -- "$ART.manifest")"
say "LIVE_VAULT_KEY_STATE=$KEY_AFTER"
say "LIVE_VAULT_KEY_ROTATED=NO"
say "LIVE_VAULT_KEY_PRINTED=NO"
say "STAGE=COMPLETE"
