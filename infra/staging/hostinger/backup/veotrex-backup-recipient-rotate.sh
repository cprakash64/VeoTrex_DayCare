#!/bin/bash
# Rotate the database-backup age recipient and prove the rotation took effect.
#
# Rotation changes WHO CAN READ future archives. It does not change the database, its password,
# the vault master key, the schedule, the retention count, or any existing archive - archives
# already written stay readable only by the identity they were written to, which is why the old
# ones are retained here and retired only after a restore from the new recipient has passed.
#
# The private half of the new recipient must never exist on this host. This script handles
# public material only: it cannot read back anything the new recipient protects.
#
# Argument 1: the new PUBLIC age recipient.
# Arguments 2..n: recipients this must NOT equal - superseded or otherwise in use elsewhere.
#                 Public material, so passing them on argv is safe.

set -euo pipefail

BAK=${VEOTREX_BACKUP_RECIPIENTS:-/etc/veotrex-daycare/backup-age-recipient.txt}
ARCHIVES=${VEOTREX_BACKUP_DEST:-/var/backups/veotrex-daycare}
ENVF=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}
UNIT=${VEOTREX_BACKUP_UNIT:-veotrex-daycare-backup.service}
TIMER=${VEOTREX_BACKUP_TIMER:-veotrex-daycare-backup.timer}

say()  { printf '%s\n' "$*"; }
fail() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }

CURRENT_STEP="startup"
step() { CURRENT_STEP=$1; printf '\n--- %s ---\n' "$*"; }
report_exit() {
    status=$?
    [ "$status" -eq 0 ] && return 0
    printf 'ABORTED: exited with status %d during %s\n' "$status" "$CURRENT_STEP" >&2
    return 0
}
trap report_exit EXIT

# find(1) fails on a missing directory and `pipefail` turns that into a silent death.
count_files() {
    [ -d "$1" ] || { printf '0\n'; return 0; }
    find "$1" -maxdepth 1 -type f -name "$2" | wc -l
}
list_archives() {
    [ -d "$ARCHIVES" ] || return 0
    find "$ARCHIVES" -maxdepth 1 -type f -name '*.dump.age' | sort
}

NEW=${1:-}
[ -n "$NEW" ] || fail "usage: $0 <new age1 recipient> [recipient-that-it-must-not-equal ...]"
shift
[ "$(id -u)" -eq 0 ] || fail "must run as root"
case "$NEW" in
    AGE-SECRET-KEY-*) fail "that is a PRIVATE identity; only the public recipient belongs here" ;;
    age1*) : ;;
    *) fail "not an age1 recipient: $NEW" ;;
esac

umask 077

# ---------------------------------------------------------------- 1. validate, and never reuse
step "STEP 1  validate the new recipient"
printf '' | age -r "$NEW" -o /dev/null 2>/dev/null || fail "age rejects this recipient: $NEW"
[ -e "$BAK" ] || fail "current recipient file not found: $BAK"
[ -f "$BAK" ] || fail "current recipient file is not a regular file: $BAK"
[ -r "$BAK" ] || fail "current recipient file is unreadable: $BAK"
[ -s "$BAK" ] || fail "current recipient file is empty: $BAK"
CURRENT=$(LC_ALL=C grep -E '^age1[0-9a-z]+$' -- "$BAK" | head -1)
[ -n "$CURRENT" ] || fail "no age1 recipient in $BAK"
[ "$NEW" != "$CURRENT" ] || fail "the new recipient is already the active one; nothing to rotate"
for forbidden in "$@"; do
    [ "$NEW" != "$forbidden" ] || fail "the new recipient equals a recipient it must not: $forbidden"
done
say "OLD_DB_BACKUP_RECIPIENT=$CURRENT"
say "NEW_DB_BACKUP_RECIPIENT=$NEW"

# The vault key is only stat'ed, and must be identical afterwards. Rotation has no business
# anywhere near it.
[ -r "$ENVF" ] || fail "deployment env file is unreadable: $ENVF"
SECRETS_DIR=$(sed -n 's/^[[:space:]]*VEOTREX_HOSTINGER_SECRETS_DIR=//p' "$ENVF" | tail -1 | tr -d '"\r')
[ -n "$SECRETS_DIR" ] || fail "VEOTREX_HOSTINGER_SECRETS_DIR is not set in $ENVF"
KEY="$SECRETS_DIR/vault_master_key"
[ -f "$KEY" ] || fail "vault master key not found: $KEY"
KEY_BEFORE=$(stat -c '%u:%g %a %s %Y' -- "$KEY")

BEFORE_LIST=$(list_archives)
BEFORE_COUNT=$(count_files "$ARCHIVES" '*.dump.age')
say "ARCHIVES_BEFORE=$BEFORE_COUNT"

# ------------------------------------------------------------------- 2. rotate, atomically
step "STEP 2  rotate the recipient"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SUPERSEDED="$BAK.superseded-$STAMP"
cp -p -- "$BAK" "$SUPERSEDED"
chmod 0600 -- "$SUPERSEDED"
printf '%s\n' "$NEW" > "$BAK.new"
chown root:root -- "$BAK.new"
chmod 0600 -- "$BAK.new"
mv -f -- "$BAK.new" "$BAK"          # atomic: no window in which the file is half-written
ACTIVE=$(LC_ALL=C grep -E '^age1[0-9a-z]+$' -- "$BAK" | head -1)
[ "$ACTIVE" = "$NEW" ] || fail "recipient file still reads $ACTIVE after rotation"
[ "$(count_files "$(dirname -- "$BAK")" "$(basename -- "$BAK")")" = "1" ] || fail "recipient file vanished"
say "RECIPIENT_FILE=$BAK (root:root $(stat -c %a -- "$BAK"))"
say "SUPERSEDED_COPY=$SUPERSEDED"

# ------------------------------------------------- 3. a real backup, through the qualified unit
step "STEP 3  fresh backup under the new recipient"
systemctl start "$UNIT" || fail "$UNIT failed; the recipient is rotated but no new archive exists"
AFTER_LIST=$(list_archives)
AFTER_COUNT=$(count_files "$ARCHIVES" '*.dump.age')
[ "$AFTER_COUNT" -eq $((BEFORE_COUNT + 1)) ] ||
    fail "expected $((BEFORE_COUNT + 1)) archives, found $AFTER_COUNT - retention may have pruned"
NEW_ARCHIVE=$(comm -13 <(printf '%s\n' "$BEFORE_LIST") <(printf '%s\n' "$AFTER_LIST"))
[ -n "$NEW_ARCHIVE" ] || fail "no new archive appeared"
[ "$(printf '%s\n' "$NEW_ARCHIVE" | wc -l)" -eq 1 ] || fail "more than one new archive appeared"

# ---------------------------------------------------------------------------- 4. report
step "STEP 4  result"
KEY_AFTER=$(stat -c '%u:%g %a %s %Y' -- "$KEY")
[ "$KEY_BEFORE" = "$KEY_AFTER" ] || fail "vault key metadata changed during rotation"
systemctl is-enabled "$TIMER" >/dev/null || fail "$TIMER is no longer enabled"
systemctl is-active  "$TIMER" >/dev/null || fail "$TIMER is no longer active"

say "NEW_ARCHIVE_PATH=$NEW_ARCHIVE"
say "NEW_ARCHIVE_SIZE=$(stat -c %s -- "$NEW_ARCHIVE")"
say "NEW_ARCHIVE_SHA256=$(sha256sum < "$NEW_ARCHIVE" | cut -d' ' -f1)"
say "NEW_ARCHIVE_MODE=$(stat -c '%U:%G %a' -- "$NEW_ARCHIVE")"
say "ARCHIVES_AFTER=$AFTER_COUNT"
say "OLD_RECIPIENT_ARCHIVES_RETAINED=$BEFORE_COUNT"
say "BACKUP_TIMER=enabled,active"
say "LIVE_VAULT_KEY_STATE=$KEY_AFTER"
say "LIVE_VAULT_KEY_ROTATED=NO"
say "ROTATION=COMPLETE"
