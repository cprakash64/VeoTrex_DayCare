"""Backup recipient rotation, exercised by running it.

Rotation decides who can read every FUTURE archive. Getting it wrong is not loud: the backups
keep succeeding and simply become unreadable by anyone who still has a key, which is discovered
at restore time, which is the worst possible time. So these cases run the real script against a
host-shaped tree and assert what it refuses, what it preserves, and what it proves afterwards.

Archives already written stay readable only by the identity they were encrypted to. The script
therefore retains them and never prunes; retiring them is a separate decision that belongs after
a restore from the new recipient has actually passed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
ROTATE = (
    REPOSITORY / "infra" / "staging" / "hostinger" / "backup" / "veotrex-backup-recipient-rotate.sh"
)

CURRENT = "age1qcurrentbackup00000000000000000000000000000000000000000cur"
NEW = "age1qnewbackuprecipient00000000000000000000000000000000000new"
DISCARDED = "age1qdiscarded00000000000000000000000000000000000000000000old"


def _user_namespace_available() -> bool:
    unshare = shutil.which("unshare")
    identity = shutil.which("id")
    if unshare is None or identity is None:
        return False
    try:
        probe = subprocess.run(  # noqa: S603 - resolved executables, fixed argv, no shell
            [unshare, "-r", identity, "-u"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "0"


pytestmark = pytest.mark.skipif(
    not _user_namespace_available(), reason="needs an unprivileged user namespace"
)

# Stands in for a copy that fails for a reason the script does not anticipate - a full disk, a
# read-only mount. Nothing in the script guards this line, so only the exit trap can report it.
CP_STUB = """#!/bin/sh
[ "${STUB_CP_RC:-0}" -ne 0 ] && exit "$STUB_CP_RC"
exec /usr/bin/cp "$@"
"""

AGE_STUB = """#!/bin/sh
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out=$a; prev=$a; done
[ -n "$out" ] && printf 'age-encryption.org/v1\\n' > "$out"
exit ${STUB_AGE_RC:-0}
"""

# `start` stands in for the qualified backup unit: it writes one new archive, as the real one
# does. Nanoseconds in the name so two runs in the same second cannot collide.
SYSTEMCTL_STUB = """#!/bin/sh
case "$1" in
  start)
    [ "${STUB_START_RC:-0}" -ne 0 ] && exit "$STUB_START_RC"
    mkdir -p "$STUB_ARCHIVES"
    printf 'archive' > "$STUB_ARCHIVES/veotrex-daycare-$(date -u +%Y%m%dT%H%M%S%NZ).dump.age"
    [ -n "${STUB_TOUCH_KEY:-}" ] && touch -d '2020-01-01 00:00:00' "$STUB_TOUCH_KEY"
    [ -n "${STUB_PRUNE:-}" ] && rm -f $(find "$STUB_ARCHIVES" -name '*.dump.age' | sort | head -1)
    exit 0 ;;
  is-enabled|is-active) exit "${STUB_TIMER_RC:-0}" ;;
esac
exit 0
"""


@pytest.fixture
def host(tmp_path: Path) -> Path:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("age", AGE_STUB), ("systemctl", SYSTEMCTL_STUB), ("cp", CP_STUB)):
        stub = binaries / name
        stub.write_text(body)
        stub.chmod(0o755)

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = secrets / "vault_master_key"
    key.write_text("c" * 64)
    key.chmod(0o600)
    (tmp_path / "hostinger.env").write_text(f"VEOTREX_HOSTINGER_SECRETS_DIR={secrets}\n")

    configuration = tmp_path / "etc"
    configuration.mkdir()
    (configuration / "backup-age-recipient.txt").write_text(f"{CURRENT}\n")

    archives = tmp_path / "archives"
    archives.mkdir()
    for stamp in ("20260915T030000Z", "20260915T150000Z", "20260916T030000Z"):
        (archives / f"veotrex-daycare-{stamp}.dump.age").write_text("old archive")
    return tmp_path


def _run(host: Path, *args: str, **overrides: str) -> tuple[int, str]:
    environment = dict(os.environ)
    environment.update(
        PATH=f"{host / 'bin'}{os.pathsep}{os.environ['PATH']}",
        VEOTREX_BACKUP_RECIPIENTS=str(host / "etc" / "backup-age-recipient.txt"),
        VEOTREX_BACKUP_DEST=str(host / "archives"),
        VEOTREX_ENV_FILE=str(host / "hostinger.env"),
        STUB_ARCHIVES=str(host / "archives"),
    )
    environment.update(overrides)
    unshare = shutil.which("unshare")
    bash = shutil.which("bash")
    assert unshare is not None and bash is not None
    result = subprocess.run(  # noqa: S603 - resolved interpreter, fixed argv, no shell
        [unshare, "-r", bash, str(ROTATE), *args],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result.returncode, result.stdout + result.stderr


def _recipient(host: Path) -> str:
    return (host / "etc" / "backup-age-recipient.txt").read_text().strip()


def _archives(host: Path) -> list[Path]:
    return sorted((host / "archives").glob("*.dump.age"))


# ----------------------------------------------------------------------------- the happy path
def test_rotation_replaces_the_recipient_and_produces_exactly_one_new_archive(host: Path) -> None:
    before = _archives(host)
    status, output = _run(host, NEW, CURRENT, DISCARDED)
    assert status == 0, output
    assert "ROTATION=COMPLETE" in output
    assert _recipient(host) == NEW
    after = _archives(host)
    assert len(after) == len(before) + 1, "exactly one new archive"
    assert all(path in after for path in before), "no existing archive may be removed"
    assert f"OLD_DB_BACKUP_RECIPIENT={CURRENT}" in output
    assert f"NEW_DB_BACKUP_RECIPIENT={NEW}" in output
    assert "NEW_ARCHIVE_SHA256=" in output
    assert "LIVE_VAULT_KEY_ROTATED=NO" in output


def test_the_superseded_recipient_is_kept_for_the_record(host: Path) -> None:
    _run(host, NEW)
    kept = list((host / "etc").glob("backup-age-recipient.txt.superseded-*"))
    assert len(kept) == 1, "the old recipient is what the retained archives are readable by"
    assert kept[0].read_text().strip() == CURRENT


# ------------------------------------------------------------------------------- the refusals
def test_rotating_to_the_recipient_already_in_use_is_refused(host: Path) -> None:
    status, output = _run(host, CURRENT)
    assert "already the active one" in output
    assert status != 0
    assert _recipient(host) == CURRENT


def test_a_recipient_on_the_forbidden_list_is_refused(host: Path) -> None:
    status, output = _run(host, DISCARDED, CURRENT, DISCARDED)
    assert "must not" in output
    assert status != 0
    assert _recipient(host) == CURRENT


def test_a_recipient_age_rejects_is_refused(host: Path) -> None:
    status, output = _run(host, NEW, STUB_AGE_RC="1")
    assert "age rejects this recipient" in output
    assert status != 0
    assert _recipient(host) == CURRENT


def test_a_private_identity_is_refused(host: Path) -> None:
    status, output = _run(host, "AGE-SECRET-KEY-1EXAMPLE")
    assert "PRIVATE identity" in output
    assert status != 0
    assert _recipient(host) == CURRENT


def test_a_missing_recipient_file_is_refused(host: Path) -> None:
    (host / "etc" / "backup-age-recipient.txt").unlink()
    status, output = _run(host, NEW)
    assert "current recipient file not found" in output
    assert status != 0


# --------------------------------------------------------- failures after the point of no return
def test_a_failing_backup_unit_says_the_recipient_is_already_rotated(host: Path) -> None:
    """The operator must know the rotation stands even though no archive was produced."""
    status, output = _run(host, NEW, STUB_START_RC="1")
    assert "the recipient is rotated but no new archive exists" in output
    assert status != 0
    assert _recipient(host) == NEW, "the rotation is not silently reverted"


def test_pruning_an_old_archive_during_rotation_is_refused(host: Path) -> None:
    """Retention must not eat an archive that is still the only copy the old identity can read."""
    status, output = _run(host, NEW, STUB_PRUNE="1")
    assert "retention may have pruned" in output
    assert status != 0


def test_touching_the_vault_key_during_rotation_is_refused(host: Path) -> None:
    key = host / "secrets" / "vault_master_key"
    status, output = _run(host, NEW, STUB_TOUCH_KEY=str(key))
    assert "vault key metadata changed" in output
    assert status != 0


def test_no_exit_can_be_silent(host: Path) -> None:
    status, output = _run(host, "not-a-recipient")
    assert status != 0
    assert "REFUSED" in output or "ABORTED" in output


def test_an_unanticipated_failure_still_names_the_step_it_died_in(host: Path) -> None:
    """`set -e` exits without a word. Only the trap distinguishes "died" from "finished"."""
    status, output = _run(host, NEW, STUB_CP_RC="1")
    assert status != 0
    assert "ABORTED: exited with status" in output, output
    assert "STEP 2" in output, "the operator must be told how far the rotation got"
