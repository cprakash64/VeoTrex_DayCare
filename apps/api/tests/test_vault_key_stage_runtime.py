"""The combined custody run, exercised by RUNNING it.

This module exists because of a specific failure: PHASE 5 printed its header and the process
returned to the shell with no prompt, no error and no phase 6. Under `pipefail`, `find` on the
not-yet-existing escrow directory failed, the substitution that counted artefacts inherited that
status, and `set -e` exited without a word - before the gate was ever invoked. Every simulation
had pre-created that directory, so the fixture was more complete than the host and the bug was
invisible. These cases run the real scripts against a host-shaped tree that is deliberately
incomplete.

A run may end in exactly two ways: it reaches the confirmation marker, or it says why it did not.
Silence is the one outcome that must be impossible.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
VAULT_KEY_DIR = REPOSITORY / "infra" / "staging" / "hostinger" / "vault-key"
STAGE = VAULT_KEY_DIR / "veotrex-vault-key-stage.sh"
GATE = VAULT_KEY_DIR / "veotrex-vault-key-escrow.sh"

RECIPIENT = "age1qtestrecovery000000000000000000000000000000000000000000rcv"
BACKUP = "age1qtestbackup0000000000000000000000000000000000000000000bkp"
CONFIRMATION = "ESCROW VAULT KEY"


def _user_namespace_available() -> bool:
    """The run installs root-owned files, so the cases need a root uid to be meaningful."""
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

STUBS = {
    # A deployment that is entirely healthy, so any refusal comes from the script under test.
    "docker": """#!/bin/sh
cat <<'TABLE'
NAME                         SERVICE    STATUS
veotrex-daycare-api-1        api        Up 26 hours (healthy)
veotrex-daycare-postgres-1   postgres   Up 37 hours (healthy)
veotrex-daycare-web-1        web        Up 37 hours (healthy)
TABLE
""",
    "curl": """#!/bin/sh
for a in "$@"; do case "$a" in http*) U=$a;; esac; done
case "$U" in
  *daycare.veotrex.com/health/*) printf 404 ;;
  *daycare.veotrex.com/) printf 200 ;;
  http://127.0.0.1:8100/health/*) printf 200 ;;
  http://127.0.0.1:3100/) printf 200 ;;
  *) printf 500 ;;
esac
""",
    "systemctl": "#!/bin/sh\nexit 0\n",
    # The deployment user can neither read nor write the installed gate.
    "runuser": "#!/bin/sh\nexit 1\n",
    # Writes something age-shaped wherever -o points, so the gate's own format check holds.
    "age": """#!/bin/sh
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out=$a; prev=$a; done
[ -n "$out" ] && printf 'age-encryption.org/v1\\nstub payload\\n' > "$out"
exit 0
""",
}


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host-shaped tree. The escrow output directory is deliberately ABSENT."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in STUBS.items():
        stub = binaries / name
        stub.write_text(body)
        stub.chmod(0o755)

    source = tmp_path / "repo" / "infra" / "staging" / "hostinger" / "vault-key"
    source.mkdir(parents=True)
    shutil.copy2(GATE, source / GATE.name)

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = secrets / "vault_master_key"
    key.write_text("b" * 64)
    key.chmod(0o600)

    configuration = tmp_path / "etc"
    configuration.mkdir()
    (tmp_path / "hostinger.env").write_text(f"VEOTREX_HOSTINGER_SECRETS_DIR={secrets}\n")
    (configuration / "backup.txt").write_text(f"{BACKUP}\n")
    archives = tmp_path / "archives"
    archives.mkdir()
    (archives / "veotrex-daycare-20260916T030926Z.dump.age").write_text("archive")
    return tmp_path


def _environment(host: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        PATH=f"{host / 'bin'}{os.pathsep}{os.environ['PATH']}",
        VEOTREX_REPO=str(host / "repo"),
        VEOTREX_GATE=str(host / "sbin" / "veotrex-vault-key-escrow"),
        VEOTREX_ENV_FILE=str(host / "hostinger.env"),
        VEOTREX_COMPOSE_FILE=str(host / "repo" / "compose.yaml"),
        VEOTREX_VAULT_RECOVERY_RECIPIENT=str(host / "etc" / "recovery.txt"),
        VEOTREX_BACKUP_RECIPIENTS=str(host / "etc" / "backup.txt"),
        VEOTREX_VAULT_ESCROW_DIR=str(host / "escrow"),
        VEOTREX_BACKUP_DEST=str(host / "archives"),
        VEOTREX_VAULT_ESCROW_LOCK=str(host / "escrow.lock"),
    )
    return environment


def _argv(host: Path) -> list[str]:
    unshare = shutil.which("unshare")
    bash = shutil.which("bash")
    assert unshare is not None and bash is not None
    (host / "sbin").mkdir(exist_ok=True)
    return [unshare, "-r", bash, str(STAGE), RECIPIENT]


def _run(host: Path, timeout: int = 90) -> tuple[int, str]:
    """No terminal at all: stdin is closed."""
    result = subprocess.run(  # noqa: S603 - resolved interpreter, fixed argv, no shell
        _argv(host),
        env=_environment(host),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def _run_interactive(host: Path, typed: str, timeout: int = 90) -> tuple[int, str]:
    """With a controlling terminal, so the gate's read from /dev/tty behaves as it does for an
    operator. Input is always supplied: a pty never reaches EOF on its own."""
    master, slave = os.openpty()
    process = subprocess.Popen(  # noqa: S603 - resolved interpreter, fixed argv, no shell
        _argv(host),
        env=_environment(host),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    os.write(master, typed.encode())
    chunks: list[bytes] = []
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO once every slave is closed: the child is finished
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(master)
    process.wait(timeout=timeout)
    return process.returncode, b"".join(chunks).decode(errors="replace").replace("\r\n", "\n")


def _artefacts(host: Path) -> list[Path]:
    escrow = host / "escrow"
    return sorted(escrow.glob("*.age")) if escrow.is_dir() else []


# ------------------------------------------------------------------ the regression itself
def test_an_absent_escrow_directory_does_not_end_the_run_in_silence(host: Path) -> None:
    """The exact failure: PHASE 5 header, then nothing.

    An absent directory is an answer of zero, not an error.
    """
    assert not (host / "escrow").exists(), "the fixture must not pre-create it"
    _, output = _run(host)
    assert "PHASE 5" in output
    assert "PHASE5_CONFIRMATION_REQUIRED" in output, (
        "the run must reach the confirmation, not exit between the header and the gate"
    )


def test_no_run_can_end_without_saying_why(host: Path) -> None:
    status, output = _run(host)
    assert status != 0
    assert "no terminal available" in output, output
    assert "ABORTED" in output or "REFUSED" in output, "a non-zero exit must name a reason"


# ------------------------------------------------------------------ the confirmation itself
def test_the_confirmation_is_announced_before_anything_blocks(host: Path) -> None:
    _, output = _run(host)
    required = output.index("PHASE5_CONFIRMATION_REQUIRED")
    assert f"Type exactly: {CONFIRMATION}" in output
    assert required < output.index("no terminal available"), "announce, then block"


def test_typing_the_phrase_accepts_and_completes(host: Path) -> None:
    status, output = _run_interactive(host, f"{CONFIRMATION}\n")
    assert "PHASE5_CONFIRMATION_ACCEPTED" in output, output
    assert "STAGE=COMPLETE" in output, output
    assert status == 0
    assert len(_artefacts(host)) == 1


def test_a_wrong_phrase_refuses_and_the_child_failure_is_surfaced(host: Path) -> None:
    status, output = _run_interactive(host, "yes\n")
    assert "confirmation did not match" in output, output
    assert "the escrow gate exited" in output, "the orchestrator must report the gate's status"
    assert status != 0
    assert not _artefacts(host)


# ------------------------------------------------------------------ refusals come first
def test_a_precondition_failure_refuses_before_the_confirmation(host: Path) -> None:
    (host / "etc" / "backup.txt").unlink()
    status, output = _run(host)
    assert "PHASE5_CONFIRMATION_REQUIRED" not in output, "never ask a human to authorise a bad run"
    assert "database-backup recipient" in output
    assert status != 0
    assert not _artefacts(host)


def test_a_rerun_reports_the_existing_artefact_and_writes_nothing_new(host: Path) -> None:
    status, output = _run_interactive(host, f"{CONFIRMATION}\n")
    assert status == 0
    first = _artefacts(host)
    assert len(first) == 1
    digest = first[0].read_bytes()

    status, output = _run(host)  # no terminal: a second escrow would block or fail
    assert status == 0, output
    assert "already exists" in output
    assert "STAGE=COMPLETE" in output
    assert [path.read_bytes() for path in _artefacts(host)] == [digest], "untouched"


def test_a_real_private_identity_beside_the_recipient_is_refused(host: Path) -> None:
    (host / "etc" / "stray-identity.txt").write_text(
        "# created by mistake\nAGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n"
    )
    status, output = _run(host)
    assert "private age material present" in output, output
    assert "PHASE5_CONFIRMATION_REQUIRED" not in output
    assert status != 0
    assert not _artefacts(host)


def test_merely_naming_the_identity_format_is_not_private_material(host: Path) -> None:
    """A check that cries wolf on its own documentation is a check that gets switched off."""
    (host / "etc" / "notes.md").write_text(
        "The gate refuses any file containing AGE-SECRET-KEY- material. See the runbook.\n"
    )
    _, output = _run(host)
    assert "private age material present" not in output, output
    assert "PHASE5_CONFIRMATION_REQUIRED" in output
