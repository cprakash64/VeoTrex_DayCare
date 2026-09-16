"""The escrow gate's separation contract, exercised by RUNNING the gate.

Reading the source cannot distinguish "the check ran and passed" from "the check was skipped",
and that distinction is the whole bug this module exists for: the reuse check used to begin
`[ -r "$BACKUP_RECIPIENT" ] &&`, so a missing or unreadable backup recipient silently became an
approval. Every case below runs the real script with a stubbed `id`, so it believes it is root
without the suite needing privilege, and asserts what it refuses and that it wrote nothing.

`age` is stubbed too. No case here reaches encryption - the point is what happens first.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
GATE = REPOSITORY / "infra" / "staging" / "hostinger" / "vault-key" / "veotrex-vault-key-escrow.sh"

# Syntactically valid for the gate's own recipient grammar. Deliberately not the deployment's
# real public recipients: a test fixture should not pin production key material.
RECOVERY = "age1qtestrecovery000000000000000000000000000000000000000000rcv"
BACKUP = "age1qtestbackup0000000000000000000000000000000000000000000bkp"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A deployment-shaped tree: env file, secrets directory, both recipient files."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # `id -u` reports root; everything else defers, so the stub cannot hide a real difference.
    _stub(
        binaries,
        "id",
        '#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n',
    )
    _stub(binaries, "age", "#!/bin/sh\nexit 0\n")

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = secrets / "vault_master_key"
    key.write_text("a" * 64)  # 64 hex characters, the form this deployment uses
    key.chmod(0o600)

    (tmp_path / "hostinger.env").write_text(f"VEOTREX_HOSTINGER_SECRETS_DIR={secrets}\n")
    (tmp_path / "recovery.txt").write_text(f"{RECOVERY}\n")
    (tmp_path / "backup.txt").write_text(f"{BACKUP}\n")
    (tmp_path / "out").mkdir()
    return tmp_path


def _run(sandbox: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        PATH=f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
        VEOTREX_ENV_FILE=str(sandbox / "hostinger.env"),
        VEOTREX_VAULT_RECOVERY_RECIPIENT=str(sandbox / "recovery.txt"),
        VEOTREX_BACKUP_RECIPIENTS=str(sandbox / "backup.txt"),
        VEOTREX_VAULT_ESCROW_DIR=str(sandbox / "out"),
        VEOTREX_VAULT_ESCROW_LOCK=str(sandbox / "escrow.lock"),
    )
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603 - resolved interpreter, fixed argv, no shell
        [bash, str(GATE)],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _artefacts(sandbox: Path) -> list[Path]:
    return sorted((sandbox / "out").glob("*.age"))


# ------------------------------------------------------- the separation check actually runs
def test_readable_and_different_recipient_reaches_the_human_gate(sandbox: Path) -> None:
    """Passing separation must lead to the confirmation prompt, not past it."""
    result = _run(sandbox)
    output = result.stdout + result.stderr
    assert "no terminal available" in output, output
    assert "REFUSED: database-backup" not in output
    assert not _artefacts(sandbox), "nothing may be written before a human confirms"


def test_recovery_recipient_equal_to_the_backup_recipient_is_refused(sandbox: Path) -> None:
    (sandbox / "backup.txt").write_text(f"{RECOVERY}\n")
    result = _run(sandbox)
    assert "is the database-backup recipient" in result.stdout + result.stderr
    assert result.returncode == 1
    assert not _artefacts(sandbox)


# ------------------------------------------------------- unreadable is never "not equal"
def test_missing_backup_recipient_is_refused(sandbox: Path) -> None:
    (sandbox / "backup.txt").unlink()
    result = _run(sandbox)
    assert "database-backup recipient not found" in result.stdout + result.stderr
    assert not _artefacts(sandbox)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the read permission bit")
def test_unreadable_backup_recipient_is_refused(sandbox: Path) -> None:
    (sandbox / "backup.txt").chmod(0o000)
    result = _run(sandbox)
    assert "database-backup recipient is unreadable" in result.stdout + result.stderr
    assert not _artefacts(sandbox)


def test_backup_recipient_path_that_is_a_directory_is_refused(sandbox: Path) -> None:
    (sandbox / "backup.txt").unlink()
    (sandbox / "backup.txt").mkdir()
    result = _run(sandbox)
    assert "not a regular file" in result.stdout + result.stderr
    assert not _artefacts(sandbox)


def test_empty_backup_recipient_is_refused(sandbox: Path) -> None:
    (sandbox / "backup.txt").write_text("")
    result = _run(sandbox)
    assert "database-backup recipient is empty" in result.stdout + result.stderr
    assert not _artefacts(sandbox)


def test_malformed_backup_recipient_is_refused(sandbox: Path) -> None:
    """A file with no age1 recipient cannot answer the question, so it is not allowed to."""
    (sandbox / "backup.txt").write_text("# rotated 2026-01-01\nnot-a-recipient\n")
    result = _run(sandbox)
    assert "holds no age1 recipient" in result.stdout + result.stderr
    assert not _artefacts(sandbox)


def test_a_recipient_age_itself_rejects_is_refused_before_the_human_gate(sandbox: Path) -> None:
    """Right shape, wrong checksum: caught before the operator is asked to authorise anything."""
    _stub(sandbox / "bin", "age", "#!/bin/sh\nexit 1\n")
    result = _run(sandbox)
    output = result.stdout + result.stderr
    assert "not a valid age recipient" in output, output
    assert "no terminal available" not in output, "must refuse before the confirmation prompt"
    assert not _artefacts(sandbox)
