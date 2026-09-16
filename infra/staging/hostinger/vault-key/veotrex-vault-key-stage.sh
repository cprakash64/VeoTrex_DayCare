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

GATE_SHA256=2cdf41d0252a2d882a100c67a6263923206ae1d7ceede87ad5b4283757e9c0f0
REPO=${VEOTREX_REPO:-/srv/veotrex-daycare/repo}
SRC="$REPO/infra/staging/hostinger/vault-key/veotrex-vault-key-escrow.sh"
GATE=${VEOTREX_GATE:-/usr/local/sbin/veotrex-vault-key-escrow}
ENVF=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}
COMPOSE=${VEOTREX_COMPOSE_FILE:-$REPO/infra/staging/hostinger/compose.yaml}
REC=${VEOTREX_VAULT_RECOVERY_RECIPIENT:-/etc/veotrex-daycare/vault-recovery-recipient.txt}
BAK=${VEOTREX_BACKUP_RECIPIENTS:-/etc/veotrex-daycare/backup-age-recipient.txt}
OUT=${VEOTREX_VAULT_ESCROW_DIR:-/root/veotrex-vault-escrow}
ORIGIN=${VEOTREX_ORIGIN_URL:-https://daycare.veotrex.com}
API_LOCAL=${VEOTREX_API_LOCAL_URL:-http://127.0.0.1:8100}
WEB_LOCAL=${VEOTREX_WEB_LOCAL_URL:-http://127.0.0.1:3100}
ARCHIVES=${VEOTREX_BACKUP_DEST:-/var/backups/veotrex-daycare}
DEPLOY_USER=${VEOTREX_DEPLOY_USER:-veotrex}
TIMER=veotrex-daycare-backup.timer

say()  { printf '%s\n' "$*"; }
fail() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }

# `set -e` exits without a word, and a run that dies between two phases looks exactly like one
# that is waiting for a human. Every non-zero exit now names the phase it died in.
CURRENT_PHASE="startup"
phase() { CURRENT_PHASE=$1; printf '\n--- %s ---\n' "$*"; }
report_exit() {
    status=$?
    [ "$status" -eq 0 ] && return 0
    printf 'ABORTED: exited with status %d during %s\n' "$status" "$CURRENT_PHASE" >&2
    return 0
}
trap report_exit EXIT

# find(1) fails when its starting point does not exist, and under `pipefail` that failure
# propagates out of a $( ... | wc -l ) substitution and kills the script. An absent directory is
# a legitimate answer of zero, not an error, so it is answered rather than raised.
count_files() {  # directory glob
    [ -d "$1" ] || { printf '0\n'; return 0; }
    find "$1" -maxdepth 1 -type f -name "$2" | wc -l
}

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
[ -r "$ENVF" ] || fail "deployment env file is unreadable: $ENVF"
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
PS=$(docker compose --env-file "$ENVF" -f "$COMPOSE" ps) || fail "docker compose ps failed"
printf '%s\n' "$PS"
for service in postgres api web; do
    printf '%s\n' "$PS" | grep -qE -- "-$service-1 .*\(healthy\)" ||
        fail "$service is not reporting healthy"
done
say "CONTAINERS=postgres,api,web all healthy"

probe() {  # url expected-status label
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$1") ||
        fail "could not reach $1"
    [ "$code" = "$2" ] || fail "$1 returned $code, expected $2"
    say "HTTP $code $3"
}

# The public origin serves the web app. The health endpoints are NOT public: nginx answers
# /health/ with 404 on purpose, so probing them through the origin tests the wrong thing.
# They are reached over loopback, where only this host can.
probe "$ORIGIN/" 200 "public origin"
probe "$ORIGIN/health/live" 404 "health stays private at the edge"
probe "$API_LOCAL/health/live" 200 "api liveness (loopback)"
probe "$API_LOCAL/health/ready" 200 "api readiness incl. PostgreSQL (loopback)"
probe "$WEB_LOCAL/" 200 "web (loopback)"
systemctl is-enabled "$TIMER" >/dev/null || fail "$TIMER is not enabled"
systemctl is-active  "$TIMER" >/dev/null || fail "$TIMER is not active"
say "BACKUP_TIMER=enabled,active"
ARCHIVE_COUNT=$(count_files "$ARCHIVES" '*.dump.age')
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
# An age private identity is a whole line of its own, not a substring. Matching the bare
# literal flags any file that merely NAMES it - this script, the gate, the runbook - and a check
# that cries wolf on its own source is a check that gets switched off.
PRIVATE=$(grep -rlI -E '^AGE-SECRET-KEY-1[0-9A-Z]+$' "$(dirname -- "$REC")" "$OUT" 2>/dev/null || true)
[ -z "$PRIVATE" ] || fail "private age material present on this host: $PRIVATE"
say "PRIVATE_AGE_MATERIAL=absent"

# -------------------------------------------------------------- PHASE 5: live vault escrow
phase "PHASE 5  live vault escrow"
EXISTING=$(count_files "$OUT" '*.age')
if [ "$EXISTING" -gt 0 ]; then
    say "escrow artefact already exists; not creating a second one"
else
    GATE_STATUS=0
    "$GATE" || GATE_STATUS=$?
    [ "$GATE_STATUS" -eq 0 ] ||
        fail "the escrow gate exited $GATE_STATUS; its refusal is printed above this line"
fi

# ------------------------------------------------------------- PHASE 6: post-escrow report
phase "PHASE 6  result"
COUNT=$(count_files "$OUT" '*.age')
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
