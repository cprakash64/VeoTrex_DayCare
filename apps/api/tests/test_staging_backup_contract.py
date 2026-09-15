"""The staging backup mechanism, asserted as source. No PostgreSQL, Docker or age needed.

A backup script is only trustworthy if its ORDER of operations is right: pruning before the new
archive is durable destroys history on the one run that needed it, and publishing before the
plaintext is shredded leaves the database readable next to its own encrypted copy. Those are
sequencing properties, so they are checked as sequencing properties rather than by running it.

The script is deliberately executed from /usr/local/sbin rather than from the git checkout: the
checkout is owned by the unprivileged `veotrex` deployment user, and a root unit executing a file
that user can edit is a privilege-escalation path.
"""

import re
import stat
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
BACKUP_DIR = REPOSITORY / "infra" / "staging" / "hostinger" / "backup"
SCRIPT = BACKUP_DIR / "veotrex-daycare-backup.sh"
SERVICE = BACKUP_DIR / "veotrex-daycare-backup.service"
TIMER = BACKUP_DIR / "veotrex-daycare-backup.timer"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


def _active(text: str) -> list[str]:
    """Executable lines only: a comment explaining a hazard is not an occurrence of it."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _index(lines: list[str], pattern: str) -> int:
    for position, line in enumerate(lines):
        if re.search(pattern, line):
            return position
    raise AssertionError(f"no line matches {pattern!r}")


# --------------------------------------------------------------------- shape
def test_backup_assets_exist_and_script_is_executable() -> None:
    for path in (SCRIPT, SERVICE, TIMER):
        assert path.is_file(), path
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "backup script must be executable in the repo"


def test_script_fails_closed(script: str) -> None:
    assert "set -euo pipefail" in script, "an unset variable or failed pipe must abort the run"


# --------------------------------------------------------------------- credential handling
def test_no_database_credential_reaches_argv_or_environment(script: str) -> None:
    """pg_dump runs inside the container over its local socket; there is nothing to leak."""
    lines = _active(script)
    body = "\n".join(lines)
    assert "PGPASSWORD" not in body
    assert not re.search(r"pg_dump[^\n]*\s-W\b", body), "no password prompt/flag"
    assert not re.search(r"postgresql(\+psycopg)?://", body), "no DSN in the script"
    dump = lines[_index(lines, r"pg_dump")]
    assert "--format=custom" in dump, "plain SQL is not an acceptable backup format here"


def test_encryption_uses_a_recipients_file_not_an_inline_key(script: str) -> None:
    """`-R file` keeps the public key out of argv, and a private key is never referenced."""
    lines = _active(script)
    # Anchored: an error message mentioning "age recipients file" is not the encrypt call.
    encrypt = lines[_index(lines, r"^\s*age\s+-R\b")]
    assert re.search(r'age\s+-R\s+"\$RECIPIENTS"', encrypt), encrypt
    body = "\n".join(lines)
    assert "AGE-SECRET-KEY" not in body, "a private identity must never appear in source"
    assert not re.search(r"\bage\s+(-d|--decrypt)\b", body), "the backup path never decrypts"


# --------------------------------------------------------------------- ordering
def test_lock_is_acquired_before_anything_is_dumped(script: str) -> None:
    lines = _active(script)
    assert _index(lines, r"flock") < _index(lines, r"pg_dump"), "overlapping runs must be refused"


def test_plaintext_is_destroyed_before_the_archive_is_published(script: str) -> None:
    lines = _active(script)
    shred = _index(lines, r"shred -u")
    publish = _index(lines, r"^\s*mv\b")
    assert shred < publish, "the plaintext dump must not outlive the encrypted one"


def test_retention_runs_only_after_a_successful_publish(script: str) -> None:
    """A failed dump must never be the reason an old, good backup disappears."""
    lines = _active(script)
    assert _index(lines, r"^\s*mv\b") < _index(lines, r"\brm -f -- \"\$old\"")


def test_retention_never_removes_the_backup_just_written(script: str) -> None:
    lines = _active(script)
    guard = _index(lines, r'\[ "\$old" = "\$FINAL" \] && continue')
    assert guard < _index(lines, r"\brm -f -- \"\$old\"")


def test_an_existing_backup_is_never_overwritten(script: str) -> None:
    lines = _active(script)
    assert _index(lines, r'\[ -e "\$FINAL" \] && fail') < _index(lines, r"pg_dump")


def test_a_failure_part_way_through_cannot_strand_plaintext(script: str) -> None:
    assert re.search(r"trap 'rm -rf -- \"\$WORK\"' EXIT", script), "work dir must be trap-cleaned"


# --------------------------------------------------------------------- naming and destination
def test_archive_names_are_utc_timestamped_and_unambiguous(script: str) -> None:
    assert re.search(r"date -u \+%Y%m%dT%H%M%SZ", script), "UTC, sortable, no spaces"
    assert "veotrex-daycare-$STAMP.dump.age" in script


def test_destination_is_root_controlled_and_not_the_checkout_or_tmp(script: str) -> None:
    default = re.search(r"DEST=\$\{VEOTREX_BACKUP_DEST:-([^}]+)\}", script)
    assert default, "the destination must have an explicit default"
    path = default.group(1)
    assert path == "/var/backups/veotrex-daycare", path
    assert not path.startswith("/tmp"), "transient storage is not backup storage"  # noqa: S108
    assert "/srv/veotrex-daycare/repo" not in path, "backups must not land in the git checkout"


# --------------------------------------------------------------------- scheduling
def test_service_runs_the_root_owned_copy_not_the_checkout() -> None:
    unit = SERVICE.read_text()
    exec_line = next(ln for ln in _active(unit) if ln.startswith("ExecStart="))
    assert exec_line == "ExecStart=/usr/local/sbin/veotrex-daycare-backup", exec_line
    assert "/srv/veotrex-daycare/repo" not in exec_line, (
        "the checkout is writable by the unprivileged deployment user; root must not execute it"
    )
    assert "Type=oneshot" in unit
    assert "NoNewPrivileges=yes" in unit


def test_timer_survives_reboot_and_is_installed() -> None:
    unit = TIMER.read_text()
    assert re.search(r"^OnCalendar=", unit, re.MULTILINE), "must be calendar-scheduled"
    assert "Persistent=true" in unit, "a missed firing must run after boot, not be skipped"
    assert "WantedBy=timers.target" in unit
    assert re.search(r"^RandomizedDelaySec=", unit, re.MULTILINE)


def test_schedule_is_at_least_daily_and_not_sub_hourly() -> None:
    unit = TIMER.read_text()
    calendar = re.search(r"^OnCalendar=(.+)$", unit, re.MULTILINE)
    assert calendar
    value = calendar.group(1)
    assert "*:*" not in value and "minutely" not in value, f"sub-hourly schedule: {value}"
    hours = re.search(r"\s(\d{2}(?:,\d{2})*):00:00", value)
    assert hours, f"expected explicit hour(s) in {value}"
    assert len(hours.group(1).split(",")) >= 1


# --------------------------------------------------------------------- operational documentation
def test_runbook_states_that_recovery_needs_the_vault_key_too() -> None:
    """A restored database is rows of undecryptable ciphertext without the matching key.

    Someone recovering from disaster reads the runbook, not this test. If it does not say that a
    dump alone is insufficient, the first real recovery discovers it at the worst moment.
    """
    runbook = (REPOSITORY / "infra" / "staging" / "hostinger" / "README.md").read_text().lower()
    assert "vault_master_key" in runbook or "vault master key" in runbook
    assert "not sufficient" in runbook or "alone is not" in runbook
    assert "both" in runbook, "the runbook must state that BOTH artefacts are required"


def test_runbook_does_not_claim_same_disk_copies_are_disaster_recovery() -> None:
    runbook = (REPOSITORY / "infra" / "staging" / "hostinger" / "README.md").read_text().lower()
    assert "same disk" in runbook
    assert "off-host" in runbook, "the limits of local archives must be stated explicitly"
