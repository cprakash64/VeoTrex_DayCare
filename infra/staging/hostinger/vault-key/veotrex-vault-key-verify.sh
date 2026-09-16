#!/bin/bash
# VeoTrex Childcare: OFF-HOST verification of an escrowed vault master key.
#
# Run this on the trusted operator machine - NEVER on the VPS, because it needs the private
# recovery identity, and that identity must never exist on the host it protects.
#
# An escrow copy that has not been decrypted is not a backup, it is a hope. This script performs
# the decryption for real and proves three things:
#   1. the artefact decrypts with the off-host recovery identity;
#   2. the recovered bytes are the key the VPS is actually running (fingerprint match);
#   3. the recovered key is structurally usable as the AES-256-GCM master key.
#
# The recovered key is written only inside a private, RAM-backed work directory where one is
# available, is never printed, and is shredded on every exit path.
#
# Usage: veotrex-vault-key-verify.sh <artefact.age> <recovery-identity> <expected-fingerprint>
#   <recovery-identity> may be a plain age identity file or a passphrase-wrapped one (.age);
#   a wrapped identity is unwrapped into the work directory and shredded with everything else.

set -euo pipefail

usage() { echo "usage: $0 <artefact.age> <recovery-identity> <expected-fingerprint>" >&2; exit 2; }
log()   { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail()  { log "FAILED: $*"; exit 1; }

[ $# -eq 3 ] || usage
ARTEFACT=$1
IDENTITY_IN=$2
EXPECTED=$3

command -v age >/dev/null 2>&1 || fail "age is not installed on this machine"

# --- 0. this must not be the host it protects -------------------------------------------------
# The verifier needs the private recovery identity. Running it on the VPS would put that identity
# on the machine whose compromise it is supposed to survive, so the marks of that host are a
# hard refusal rather than a warning in a runbook.
ENV_FILE=${VEOTREX_ENV_FILE:-/etc/veotrex-daycare/hostinger.env}
for marker in \
    "$ENV_FILE" \
    "${VEOTREX_HOSTINGER_SECRETS_DIR:-/etc/veotrex-daycare/secrets}/vault_master_key" \
    /usr/local/sbin/veotrex-vault-key-escrow \
    /etc/veotrex-daycare/vault-recovery-recipient.txt
do
    if [ -e "$marker" ]; then
        fail "$marker exists: this looks like the VeoTrex host, and the recovery identity must never reach it"
    fi
done
[ -r "$ARTEFACT" ]    || fail "artefact is unreadable: $ARTEFACT"
[ -r "$IDENTITY_IN" ] || fail "recovery identity is unreadable: $IDENTITY_IN"
head -c 22 -- "$ARTEFACT" | grep -q 'age-encryption.org' || fail "artefact is not an age file"

umask 077
# /dev/shm is RAM-backed on Linux, so the plaintext key never reaches a persistent filesystem.
# Elsewhere this falls back to the system temporary directory; shredding then matters more.
RAMDIR=""
for candidate in "${XDG_RUNTIME_DIR:-}" /dev/shm; do
    [ -n "$candidate" ] && [ -d "$candidate" ] && [ -w "$candidate" ] && { RAMDIR=$candidate; break; }
done
WORK=$(mktemp -d "${RAMDIR:-${TMPDIR:-/tmp}}/veotrex-vault-verify.XXXXXXXX") \
    || fail "cannot create work directory"
[ -n "$RAMDIR" ] || log "NOTE: no RAM-backed directory found; plaintext will touch disk and is shredded on exit"
chmod 0700 -- "$WORK"
cleanup() {
    find "$WORK" -type f -exec shred -u -- {} + 2>/dev/null || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT INT TERM

IDENTITY="$IDENTITY_IN"
if head -c 22 -- "$IDENTITY_IN" | grep -q 'age-encryption.org'; then
    log "recovery identity is passphrase-wrapped; unwrapping into the work directory"
    age -d -o "$WORK/identity" -- "$IDENTITY_IN" || fail "could not unwrap the recovery identity"
    IDENTITY="$WORK/identity"
fi
LC_ALL=C grep -q 'AGE-SECRET-KEY-' -- "$IDENTITY" || fail "that file is not an age private identity"

# --- 1. it actually decrypts -----------------------------------------------------------------
age -d -i "$IDENTITY" -o "$WORK/key" -- "$ARTEFACT" \
    || fail "decryption failed - this artefact is NOT recoverable with this identity"
[ -s "$WORK/key" ] || fail "decryption produced an empty file"

# --- 2. it is the key the VPS is running ------------------------------------------------------
ACTUAL=$(sha256sum < "$WORK/key" | cut -c1-16)
[ "$ACTUAL" = "$EXPECTED" ] \
    || fail "fingerprint mismatch: expected $EXPECTED, recovered $ACTUAL - this is not the live key"

# --- 3. it is structurally a 32-byte AEAD key -------------------------------------------------
if tr -d '[:space:]' < "$WORK/key" | LC_ALL=C grep -qE '^[0-9a-fA-F]{64}$'; then
    KEY_FORM=hex
elif [ "$(tr -d '[:space:]' < "$WORK/key" | base64 -d 2>/dev/null | wc -c)" -eq 32 ]; then
    KEY_FORM=base64
else
    fail "recovered key is neither 64 hex characters nor 32 base64-decoded bytes"
fi

# Functional check against the same primitive the vault uses. The key is piped in on stdin, so
# it never reaches argv; the script prints only a verdict.
if python3 -c 'import cryptography' >/dev/null 2>&1; then
    tr -d '[:space:]' < "$WORK/key" | python3 -c '
import base64, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

raw = sys.stdin.buffer.read().strip()
key = bytes.fromhex(raw.decode()) if len(raw) == 64 and not set(raw) - set(b"0123456789abcdefABCDEF") else base64.b64decode(raw, validate=True)
assert len(key) == 32, "master key must be 32 bytes"
aead, nonce, aad = AESGCM(key), b"\x00" * 12, b"veotrex-vault-key-verify"
assert aead.decrypt(nonce, aead.encrypt(nonce, b"round-trip", aad), aad) == b"round-trip"
print("AES-256-GCM round-trip: ok")
' || fail "recovered key failed an AES-256-GCM round-trip"
else
    log "NOTE: python cryptography unavailable; structural check only, no AEAD round-trip"
fi

log "PASS  artefact=$(basename -- "$ARTEFACT") form=$KEY_FORM fingerprint=$ACTUAL"
log "The escrowed vault master key is recoverable off-host. Now remove the copy left on the VPS."
