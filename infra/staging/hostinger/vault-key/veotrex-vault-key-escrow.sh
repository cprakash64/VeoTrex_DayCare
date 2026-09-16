#!/bin/bash
# VeoTrex Childcare: bounded root gate that publishes ONE encrypted, off-host-recoverable copy
# of the EXISTING vault master key.
#
# What this script does NOT do, by construction:
#   - it does not rotate, regenerate or modify the live key;
#   - it never prints the key, echoes it, or places it in argv, the environment or shell history
#     (age reads the secret file directly by path - the bytes never pass through this shell);
#   - it never writes a plaintext copy: the only artefact it creates is age ciphertext;
#   - it cannot read back what it writes. The recovery recipient is a PUBLIC age key whose
#     private identity is generated off-host and must never exist on this VPS.
#
# The recovery recipient MUST be a different key pair from the database-backup recipient. One
# identity that unlocks both the archives and the key that makes them meaningful is a single
# point of total compromise; the script refuses to run if the two recipients are the same.
#
# Run as root, from a root-owned path, by hand. There is no timer: this is a custody event, not
# a schedule.

set -euo pipefail

log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "REFUSED: $*"; exit 1; }

ENV_FILE=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}

# The deployment's own env file is the authority for where the secrets live - compose reads
# VEOTREX_HOSTINGER_SECRETS_DIR from exactly this file. A gate carrying its own guess escrows
# whatever happens to sit at that guess, or nothing; under snap Docker, which cannot bind-mount
# arbitrary host paths, the real directory is not the one the example file documents.
env_secrets_dir() {
    [ -r "$ENV_FILE" ] || return 0
    sed -n 's/^[[:space:]]*VEOTREX_HOSTINGER_SECRETS_DIR=//p' "$ENV_FILE" |
        tail -1 | tr -d '"'"'"'\r'
}
SECRETS_DIR=${VEOTREX_HOSTINGER_SECRETS_DIR:-$(env_secrets_dir)}
[ -n "$SECRETS_DIR" ] || fail "VEOTREX_HOSTINGER_SECRETS_DIR is not set in $ENV_FILE"
KEY_FILE=${VEOTREX_VAULT_KEY_FILE:-$SECRETS_DIR/vault_master_key}
RECIPIENT=${VEOTREX_VAULT_RECOVERY_RECIPIENT:-/etc/veotrex-daycare/vault-recovery-recipient.txt}
BACKUP_RECIPIENT=${VEOTREX_BACKUP_RECIPIENTS:-/etc/veotrex-daycare/backup-age-recipient.txt}
OUT_DIR=${VEOTREX_VAULT_ESCROW_DIR:-/root/veotrex-vault-escrow}
LOCK=${VEOTREX_VAULT_ESCROW_LOCK:-/var/lock/veotrex-vault-key-escrow.lock}
CONFIRMATION="ESCROW VAULT KEY"

# --- 1. privilege and concurrency bounds -----------------------------------------------------
[ "$(id -u)" -eq 0 ] || fail "must run as root; the key file is root-owned 0600"

exec 9>"$LOCK" || fail "cannot open lock $LOCK"
flock -n 9 || fail "another escrow run holds the lock"

umask 077

# --- 2. the live key must exist, be sound, and be no more readable than it already is ---------
[ -f "$KEY_FILE" ] || fail "vault master key file not found: $KEY_FILE"
[ -s "$KEY_FILE" ] || fail "vault master key file is empty: $KEY_FILE"
KEY_MODE=$(stat -c %a -- "$KEY_FILE")
[ "$KEY_MODE" = "600" ] || fail "vault master key is mode $KEY_MODE; expected 600 - fix custody first"

# Mirrors VaultKeyProvider._decode_key: 64 hex characters, or base64 decoding to exactly 32
# bytes. Both checks consume the key through a pipe with output suppressed or counted; neither
# branch can put a byte of it on the terminal.
if tr -d '[:space:]' < "$KEY_FILE" | LC_ALL=C grep -qE '^[0-9a-fA-F]{64}$'; then
    KEY_FORM=hex
elif [ "$(tr -d '[:space:]' < "$KEY_FILE" | base64 -d 2>/dev/null | wc -c)" -eq 32 ]; then
    KEY_FORM=base64
else
    fail "vault master key is neither 64 hex characters nor 32 base64-decoded bytes"
fi

# A truncated digest of the key FILE. It is a commitment, not the key: 256 bits of preimage
# resistance over 32 random bytes. It exists so the off-host verifier can prove that what it
# decrypted is the key this host is actually running, without either side transmitting the key.
FINGERPRINT=$(sha256sum < "$KEY_FILE" | cut -c1-16)

# --- 3. the recovery recipient: public, well-formed, and NOT the backup recipient -------------
[ -r "$RECIPIENT" ] || fail "recovery recipient file is unreadable: $RECIPIENT"
[ -s "$RECIPIENT" ] || fail "recovery recipient file is empty: $RECIPIENT"
if LC_ALL=C grep -q 'AGE-SECRET-KEY-' -- "$RECIPIENT"; then
    fail "$RECIPIENT contains a PRIVATE age identity; private recovery material must never reach this host"
fi
RECIPIENT_COUNT=$(LC_ALL=C grep -cE '^age1[0-9a-z]+$' -- "$RECIPIENT" || true)
[ "$RECIPIENT_COUNT" -eq 1 ] || fail "expected exactly one age1... recipient in $RECIPIENT, found $RECIPIENT_COUNT"
RECIPIENT_KEY=$(LC_ALL=C grep -E '^age1[0-9a-z]+$' -- "$RECIPIENT")

# The recovery recipient must be a recipient age itself accepts, not merely a string of the
# right shape. Checking it here, before the human is asked to confirm, means a mistyped key is
# refused up front rather than after the operator has authorised a custody event.
printf '' | age -r "$RECIPIENT_KEY" -o /dev/null 2>/dev/null ||
    fail "recovery recipient is not a valid age recipient"

# "Unreadable" is not "different". Separation is only a guarantee if the file it compares
# against is present, parseable, and actually read: every way of failing to read it is a refusal,
# never a silent pass. This check previously began with `[ -r "$BACKUP_RECIPIENT" ] &&`, which
# turned a missing or unreadable file into an unnoticed approval.
[ -e "$BACKUP_RECIPIENT" ] || fail "database-backup recipient not found: $BACKUP_RECIPIENT"
[ -f "$BACKUP_RECIPIENT" ] || fail "database-backup recipient is not a regular file: $BACKUP_RECIPIENT"
[ -r "$BACKUP_RECIPIENT" ] || fail "database-backup recipient is unreadable: $BACKUP_RECIPIENT"
[ -s "$BACKUP_RECIPIENT" ] || fail "database-backup recipient is empty: $BACKUP_RECIPIENT"
BACKUP_COUNT=$(LC_ALL=C grep -cE '^age1[0-9a-z]+$' -- "$BACKUP_RECIPIENT" || true)
[ "$BACKUP_COUNT" -ge 1 ] || fail "database-backup recipient holds no age1 recipient: $BACKUP_RECIPIENT"

if LC_ALL=C grep -qxF "$RECIPIENT_KEY" -- "$BACKUP_RECIPIENT"; then
    fail "recovery recipient is the database-backup recipient; use a separate key pair so one identity cannot unlock both the archives and the key that makes them readable"
fi

# No private identity may be sitting in the output directory either.
if [ -d "$OUT_DIR" ] && LC_ALL=C grep -rql 'AGE-SECRET-KEY-' -- "$OUT_DIR" 2>/dev/null; then
    fail "$OUT_DIR contains private age material; remove it before escrowing"
fi

mkdir -p -- "$OUT_DIR"
chmod 0700 -- "$OUT_DIR"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL="$OUT_DIR/veotrex-vault-master-key-$STAMP.age"
[ -e "$FINAL" ] && fail "artefact already exists: $FINAL"

# --- 4. the manual gate ----------------------------------------------------------------------
# Deliberately interactive. An escrow copy of the master key is a custody event that a human
# must authorise at the moment it happens; there is no unattended path.
cat >&2 <<BANNER

  About to create an ENCRYPTED off-host recovery copy of the LIVE vault master key.

    key file            $KEY_FILE  (unchanged, not rotated, not printed)
    key encoding        $KEY_FORM
    key fingerprint     $FINGERPRINT
    recovery recipient  $RECIPIENT_KEY
    artefact            $FINAL

  This host cannot decrypt what it is about to write. Confirm that the matching private
  identity exists ONLY on your trusted off-host machine before continuing.

BANNER
printf 'Type exactly "%s" to proceed: ' "$CONFIRMATION" >&2
IFS= read -r REPLY_TEXT < /dev/tty || fail "no terminal available; this gate is interactive by design"
[ "$REPLY_TEXT" = "$CONFIRMATION" ] || fail "confirmation did not match; nothing was written"

# --- 5. encrypt in place: no plaintext copy is ever created -----------------------------------
WORK=$(mktemp -d "$OUT_DIR/.work.XXXXXXXX") || fail "cannot create work directory"
trap 'rm -rf -- "$WORK"' EXIT
ENC="$WORK/key.age"

age -R "$RECIPIENT" -o "$ENC" -- "$KEY_FILE" || fail "age encryption failed"
[ -s "$ENC" ] || fail "encrypted artefact is empty"
head -c 22 -- "$ENC" | grep -q 'age-encryption.org' || fail "artefact is not an age file"

sync -- "$ENC" 2>/dev/null || true
mv -- "$ENC" "$FINAL" || fail "could not publish $FINAL"
chmod 0600 -- "$FINAL"

# Non-secret transfer manifest. Carries the commitment, never the key.
MANIFEST="$FINAL.manifest"
{
    echo "artefact=$(basename -- "$FINAL")"
    echo "created_utc=$STAMP"
    echo "key_form=$KEY_FORM"
    echo "key_fingerprint_sha256_16=$FINGERPRINT"
    echo "recovery_recipient=$RECIPIENT_KEY"
    echo "source_host=$(hostname -f 2>/dev/null || hostname)"
    echo "rotated=no"
} > "$MANIFEST"
chmod 0600 -- "$MANIFEST"

log "escrow artefact published $FINAL bytes=$(stat -c %s -- "$FINAL") fingerprint=$FINGERPRINT"

cat >&2 <<NEXT

  Next, OFF-HOST, on the trusted machine:

    scp root@<vps>:$FINAL      .
    scp root@<vps>:$MANIFEST   .
    ./veotrex-vault-key-verify.sh ./$(basename -- "$FINAL") <identity> $FINGERPRINT

  Only after the verifier reports PASS, remove the copy from this host:

    shred -u -- $FINAL $MANIFEST

  Custody is NOT qualified until the off-host decryption has actually been performed.

NEXT
