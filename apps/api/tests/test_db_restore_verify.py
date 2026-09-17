"""The off-host restore verifier, exercised by running it against a stubbed Docker.

A rotated backup recipient is an untested backup by definition: nothing has yet shown the new
key opens anything. This script is what turns that into evidence, so what it REFUSES matters as
much as what it reports - a verifier that passes on a broken archive is worse than none.

Docker is stubbed: the cases here are about the script's decisions, its isolation flags, and its
cleanup. The real container runs on the operator's machine.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
VERIFY = REPOSITORY / "infra" / "staging" / "hostinger" / "backup" / "veotrex-db-restore-verify.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

DOCKER_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG"
case "$1" in
  volume) exit 0 ;;
  run) exit 0 ;;
  cp) exit 0 ;;
  rm) exit 0 ;;
  exec)
    shift                      # drop "exec"
    [ "$1" = "--" ] && shift   # drop the argument terminator
    shift                      # drop the container name
    case "$1" in
      pg_isready) exit "${STUB_READY_RC:-0}" ;;
      createdb) exit 0 ;;
      psql)
        for a in "$@"; do LAST=$a; done
        case "$LAST" in
          *CREATE\\ ROLE*) exit 0 ;;
          *alembic_version*) printf '%s\\n' "${STUB_HEAD:-0005_encrypted_credentials}" ;;
          *information_schema.tables*) printf '%s\\n' "${STUB_TABLES:-22}" ;;
          *pg_policies*) printf '%s\\n' "${STUB_POLICIES:-14}" ;;
          *relrowsecurity*) printf '%s\\n' "${STUB_RLS:-14}" ;;
          *encrypted_credentials*)
            [ -n "${STUB_CREDS_RC:-}" ] && exit "$STUB_CREDS_RC"
            printf '0\\n' ;;
          *) printf '7\\n' ;;
        esac
        exit 0 ;;
      pg_restore)
        case "$2" in
          --list)
            printf '; alembic_version\\n; encrypted_credentials\\n'
            printf '2; TABLE DATA x\\n3; TABLE DATA y\\n'
            exit 0 ;;
        esac
        [ -n "${STUB_ERROR_LINES:-}" ] && echo "pg_restore: error: something" >&2
        exit "${STUB_RESTORE_RC:-0}" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

AGE_STUB = """#!/bin/sh
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out=$a; prev=$a; done
[ "${STUB_AGE_RC:-0}" -ne 0 ] && exit "$STUB_AGE_RC"
case "$out" in
  */identity) printf 'AGE-SECRET-KEY-1STUB\\n' > "$out" ;;
  */dump) printf '%s' "${STUB_DUMP_MAGIC:-PGDMP}" > "$out"; printf 'body' >> "$out" ;;
esac
exit 0
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("age", AGE_STUB)):
        stub = binaries / name
        stub.write_text(body)
        stub.chmod(0o755)
    archive = tmp_path / "veotrex-daycare-20260917T013302Z.dump.age"
    archive.write_text("age-encryption.org/v1\nciphertext\n")
    identity = tmp_path / "db-backup-recovery.txt.age"
    identity.write_text("age-encryption.org/v1\nwrapped identity\n")
    return tmp_path


def _sha256(path: Path) -> str:
    digest = subprocess.run(  # noqa: S603 - resolved executable, fixed argv, no shell
        [shutil.which("sha256sum") or "/usr/bin/sha256sum"],
        stdin=path.open("rb"),
        capture_output=True,
        text=True,
        check=True,
    )
    return digest.stdout.split()[0]


def _run(workspace: Path, expected_sha: str | None = None, **overrides: str) -> tuple[int, str]:
    archive = workspace / "veotrex-daycare-20260917T013302Z.dump.age"
    environment = dict(os.environ)
    environment.update(
        PATH=f"{workspace / 'bin'}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(workspace / "docker.log"),
        # The marker check must not trip on a machine that has none of these.
        VEOTREX_ENV_FILE=str(workspace / "absent.env"),
    )
    environment.update(overrides)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - resolved interpreter, fixed argv, no shell
        [
            bash,
            str(VERIFY),
            str(archive),
            str(workspace / "db-backup-recovery.txt.age"),
            expected_sha or _sha256(archive),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


def _docker_log(workspace: Path) -> str:
    log = workspace / "docker.log"
    return log.read_text() if log.exists() else ""


# --------------------------------------------------------------------------- the happy path
def test_a_good_archive_qualifies(workspace: Path) -> None:
    status, output = _run(workspace)
    assert "DB_RESTORE_VERIFY=QUALIFIED" in output, output
    assert status == 0
    assert "MIGRATION_HEAD=0005_encrypted_credentials" in output
    assert "PUBLIC_TABLES=22" in output
    assert "RLS_POLICIES=14" in output
    assert "RLS_ENABLED_TABLES=14" in output
    assert "RESTORE_ERROR_LINES=0" in output


def test_the_restore_target_is_isolated(workspace: Path) -> None:
    _run(workspace)
    invocation = next(
        line for line in _docker_log(workspace).splitlines() if line.startswith("run ")
    )
    assert "--network none" in invocation, "the scratch database must reach nothing"
    assert " -p " not in invocation and "--publish" not in invocation, "no host port"
    assert "veotrex-restore-" in invocation, "a uniquely named throwaway container"


def test_everything_scratch_is_destroyed(workspace: Path) -> None:
    _run(workspace)
    log = _docker_log(workspace)
    assert "rm -f" in log, "the container is removed"
    assert "volume rm -f" in log, "the volume is removed"


def test_cleanup_also_happens_when_the_restore_fails(workspace: Path) -> None:
    status, _ = _run(workspace, STUB_RESTORE_RC="1")
    assert status != 0
    log = _docker_log(workspace)
    assert "rm -f" in log and "volume rm -f" in log, "a failed run must not strand a database"


# ----------------------------------------------------------------------------- the refusals
def test_a_mismatched_archive_checksum_refuses_before_decrypting(workspace: Path) -> None:
    status, output = _run(workspace, expected_sha="0" * 64)
    assert "does not match the value published on the VPS" in output
    assert status != 0
    assert "run " not in _docker_log(workspace), "nothing may start before the bytes are trusted"


def test_a_failed_decryption_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_AGE_RC="1")
    assert "could not unwrap the recovery identity" in output or "decryption failed" in output
    assert status != 0


def test_a_dump_that_is_not_a_custom_format_archive_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_DUMP_MAGIC="SQL--")
    assert "not a PostgreSQL custom-format archive" in output
    assert status != 0


def test_a_nonzero_pg_restore_exit_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_RESTORE_RC="2")
    assert "pg_restore exited 2" in output
    assert status != 0


def test_any_restore_error_line_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_ERROR_LINES="1")
    assert "error line" in output
    assert status != 0


def test_a_wrong_migration_head_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_HEAD="0004_ring_inventory")
    assert "migration head mismatch" in output
    assert status != 0


def test_a_missing_table_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_TABLES="21")
    assert "public table count mismatch" in output
    assert status != 0


def test_a_missing_rls_policy_refuses(workspace: Path) -> None:
    """Tenant isolation that did not survive the round trip is a silent cross-tenant leak."""
    status, output = _run(workspace, STUB_POLICIES="13")
    assert "RLS policy count mismatch" in output
    assert status != 0


def test_a_table_with_rls_switched_off_refuses(workspace: Path) -> None:
    status, output = _run(workspace, STUB_RLS="13")
    assert "RLS-enabled table count mismatch" in output
    assert status != 0


def test_unqueryable_credentials_refuse(workspace: Path) -> None:
    status, output = _run(workspace, STUB_CREDS_RC="1")
    assert "encrypted_credentials is not queryable" in output
    assert status != 0


def test_it_refuses_to_run_on_the_deployment_host(workspace: Path) -> None:
    marker = workspace / "present.env"
    marker.write_text("VEOTREX_HOSTINGER_SECRETS_DIR=/var/snap/x\n")
    status, output = _run(workspace, VEOTREX_ENV_FILE=str(marker))
    assert "this is the deployment host" in output
    assert status != 0
