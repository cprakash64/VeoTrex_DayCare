"""The vault master key escrow gate, asserted as source. No age, root or VPS needed.

The key that makes every database archive readable is escrowed by a script whose value is
entirely in what it REFUSES to do: reveal the key, reuse the backup identity, accept private
recovery material, or run unattended. Those are source-level properties, so they are checked as
source-level properties rather than by running a gate that requires root and a terminal.

Ordering matters as much as presence. Every refusal must be reachable before anything is
encrypted or written, otherwise a check that fires after the artefact exists is decoration.
"""

import re
import stat
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
VAULT_KEY_DIR = REPOSITORY / "infra" / "staging" / "hostinger" / "vault-key"
ESCROW = VAULT_KEY_DIR / "veotrex-vault-key-escrow.sh"
VERIFY = VAULT_KEY_DIR / "veotrex-vault-key-verify.sh"
RUNBOOK = VAULT_KEY_DIR / "README.md"


@pytest.fixture(scope="module")
def escrow() -> str:
    return ESCROW.read_text()


@pytest.fixture(scope="module")
def verify() -> str:
    return VERIFY.read_text()


def _active(text: str) -> list[str]:
    """Executable lines only: a comment explaining a hazard is not an occurrence of it."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _index(lines: list[str], pattern: str) -> int:
    for position, line in enumerate(lines):
        if re.search(pattern, line):
            return position
    raise AssertionError(f"no line matches {pattern!r}")


# --------------------------------------------------------------------- shape
def test_escrow_assets_exist_and_scripts_are_executable() -> None:
    for path in (ESCROW, VERIFY, RUNBOOK):
        assert path.is_file(), path
    for script in (ESCROW, VERIFY):
        assert script.stat().st_mode & stat.S_IXUSR, f"{script.name} must be executable in the repo"


def test_scripts_fail_closed(escrow: str, verify: str) -> None:
    for script in (escrow, verify):
        assert "set -euo pipefail" in script


# --------------------------------------------------------------------- the key is never revealed
def test_the_key_never_reaches_argv_a_variable_or_the_terminal(escrow: str) -> None:
    """age reads the secret file by path; nothing ever substitutes its contents."""
    body = "\n".join(_active(escrow))
    assert not re.search(r"\$\(\s*cat[^)]*KEY_FILE", body), "the key must never enter a variable"
    assert not re.search(r"(echo|printf|cat)[^\n|]*\"\$KEY_FILE\"\s*$", body), "never print the key"
    assert 'age -R "$RECIPIENT" -o "$ENC" -- "$KEY_FILE"' in body, "encrypt the file in place"


def test_the_key_is_not_rotated_or_written(escrow: str) -> None:
    body = "\n".join(_active(escrow))
    assert not re.search(r">\s*\"?\$KEY_FILE", body), "the live key must never be written to"
    assert "age-keygen" not in body, "the gate escrows the existing key; it generates nothing"


# --------------------------------------------------------------------- identity separation
def test_backup_identity_cannot_be_reused_as_the_recovery_recipient(escrow: str) -> None:
    lines = _active(escrow)
    refusal = _index(lines, r"BACKUP_RECIPIENT.*grep -qxF")
    encryption = _index(lines, r"age -R")
    assert refusal < encryption, "the reuse check must refuse before anything is encrypted"


def test_private_recovery_material_is_refused(escrow: str) -> None:
    """Both places a private identity could arrive are refused, and both before encryption.

    One check standing in for the other is how this regresses: matching "AGE-SECRET-KEY-"
    anywhere in the file would keep passing while either refusal was deleted.
    """
    lines = _active(escrow)
    encryption = _index(lines, r"age -R")
    handed_in = _index(lines, r"grep -q 'AGE-SECRET-KEY-' -- \"\$RECIPIENT")
    left_behind = _index(lines, r"grep -rql 'AGE-SECRET-KEY-' -- \"\$OUT_DIR")
    assert handed_in < encryption, "a private identity passed as the recipient must be refused"
    assert left_behind < encryption, "a private identity sitting in the output directory too"


# --------------------------------------------------------------------- the gate itself
def test_the_gate_is_root_only_and_interactive(escrow: str) -> None:
    lines = _active(escrow)
    assert _index(lines, r"id -u.*-eq 0") < _index(lines, r"age -R")
    confirmation = _index(lines, r"read -r REPLY_TEXT < /dev/tty")
    assert confirmation < _index(lines, r"age -R"), "nothing is encrypted before a human confirms"
    assert '[ "$REPLY_TEXT" = "$CONFIRMATION" ]' in "\n".join(lines)


def test_a_loosely_permissioned_key_is_refused_rather_than_escrowed(escrow: str) -> None:
    """A key readable beyond root is a custody problem; copying it is not the fix."""
    lines = _active(escrow)
    assert _index(lines, r'\[ "\$KEY_MODE" = "600" \]') < _index(lines, r"age -R")


def test_an_existing_artefact_is_never_overwritten(escrow: str) -> None:
    lines = _active(escrow)
    assert _index(lines, r'\[ -e "\$FINAL" \] && fail') < _index(lines, r"age -R")


def test_the_artefact_is_published_atomically_and_privately(escrow: str) -> None:
    lines = _active(escrow)
    assert _index(lines, r"age -R") < _index(lines, r'mv -- "\$ENC" "\$FINAL"')
    assert _index(lines, r'mv -- "\$ENC" "\$FINAL"') < _index(lines, r'chmod 0600 -- "\$FINAL"')
    assert "umask 077" in "\n".join(lines)
    assert re.search(r"trap 'rm -rf -- \"\$WORK\"' EXIT", escrow), "no stray work directory"


def test_the_manifest_carries_a_commitment_and_not_the_key(escrow: str) -> None:
    manifest = escrow.split("MANIFEST=", 1)[1]
    assert "key_fingerprint_sha256_16=$FINGERPRINT" in manifest
    assert "cut -c1-16" in escrow, "the fingerprint is truncated; it identifies, it does not reveal"
    assert "$KEY_FILE" not in manifest.split('} > "$MANIFEST"', 1)[0]


# --------------------------------------------------------------------- off-host verification
def test_verification_decrypts_for_real_before_declaring_pass(verify: str) -> None:
    lines = _active(verify)
    decrypt = _index(lines, r'age -d -i "\$IDENTITY"')
    fingerprint = _index(lines, r'\[ "\$ACTUAL" = "\$EXPECTED" \]')
    verdict = _index(lines, r'log "PASS')
    assert decrypt < fingerprint < verdict, "PASS must follow a real decryption and a real match"


def test_recovered_plaintext_is_shredded_on_every_exit_path(verify: str) -> None:
    body = "\n".join(_active(verify))
    assert "shred -u" in body
    assert "trap cleanup EXIT INT TERM" in body, "interruption must not strand the recovered key"


def test_the_verifier_refuses_to_run_on_the_host_it_protects(verify: str) -> None:
    lines = _active(verify)
    refusal = _index(lines, r"looks like the VeoTrex host")
    assert refusal < _index(lines, r'age -d -i "\$IDENTITY"'), "refuse before using the identity"
    assert "/usr/local/sbin/veotrex-vault-key-escrow" in verify, "the installed gate is a marker"


def test_no_recovered_key_bytes_are_ever_printed(verify: str) -> None:
    body = "\n".join(_active(verify))
    assert not re.search(r"(cat|echo|printf)\s+[^|\n]*\"\$WORK/key\"", body)
    assert 'print("AES-256-GCM round-trip: ok")' in verify, "the round-trip reports a verdict only"


def test_neither_script_traces_its_own_execution(escrow: str, verify: str) -> None:
    """set -x would put every expanded argument, including file contents, on stderr."""
    for script in (escrow, verify):
        assert not re.search(r"^\s*set\s+-[a-z]*x", script, re.MULTILINE)


def test_no_ring_auth0_or_ssh_material_is_in_scope(escrow: str) -> None:
    """The gate reads exactly one secret: the vault master key."""
    body = "\n".join(_active(escrow)).lower()
    for foreign in ("ring_client_secret", "ring_hmac", "auth0", "id_rsa", "id_ed25519", "ssh-"):
        assert foreign not in body, f"{foreign} has no business in the escrow gate"


def test_the_runbook_keeps_the_two_rehearsals_distinct() -> None:
    runbook = RUNBOOK.read_text()
    assert "RESTORE_REHEARSAL" in runbook and "CREDENTIAL_RECOVERY_REHEARSAL" in runbook
    assert "sha256sum /usr/local/sbin/veotrex-vault-key-escrow" in runbook, "checksum contract"


def test_the_key_path_comes_from_the_deployment_env_file(escrow: str) -> None:
    """A gate that carries its own idea of where the secrets live escrows the wrong file.

    Compose interpolates VEOTREX_HOSTINGER_SECRETS_DIR from the deployment env file, and under
    snap Docker that directory is not the one the example env documents. Reading the same file
    compose reads is what makes "the key we escrowed" and "the key the stack mounts" the same
    statement rather than two hopes.
    """
    lines = _active(escrow)
    resolution = _index(lines, r"VEOTREX_HOSTINGER_SECRETS_DIR=//p")
    # The assignment must CONSUME the resolution. A file that merely still contains the helper
    # while SECRETS_DIR is hardcoded back to a guess is the exact regression this guards.
    assignment = _index(lines, r"^SECRETS_DIR=.*env_secrets_dir")
    assert resolution < assignment < _index(lines, r"^KEY_FILE=.*SECRETS_DIR")
    assert _index(lines, r'\[ -n "\$SECRETS_DIR" \] \|\| fail') < _index(lines, r"age -R"), (
        "an unresolvable secrets directory must refuse, not fall back to a guess"
    )


def test_failure_reporting_is_defined_before_anything_can_fail(escrow: str) -> None:
    """`fail` used above its own definition reports "command not found", not the reason."""
    lines = _active(escrow)
    assert _index(lines, r"^fail\(\)") < _index(lines, r"\|\| fail"), "hoist fail() above its uses"


def test_the_deployment_env_file_marks_the_host_for_the_verifier(verify: str) -> None:
    lines = _active(verify)
    assert _index(lines, r"^ENV_FILE=") < _index(lines, r"for marker in")
    assert '"$ENV_FILE" \\' in verify, "the env file only exists on the host being protected"
