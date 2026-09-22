"""Unprivileged lifecycle tests for infra/jetson/veotrex-edge-ctl.sh (V1-00B-R1).

The script runs against a temporary prefix (``VEOTREX_EDGE_ROOT``) in test mode, with stub
``systemctl``, ``systemd-analyze``, ``useradd``, ``usermod``, ``groupadd``, ``getent``, ``id``
and ``journalctl`` commands on PATH that record every invocation and emulate the exact host
behaviours that mattered on the real Jetson: ``systemd-analyze verify`` fails with
"Command ... is not executable: No such file or directory" whenever the ExecStart target does
not exist; ``is-enabled`` reflects ``enable``/``disable``; ``daemon-reload`` can be made to
fail. No root, no real systemd state, no network.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
CTL = REPOSITORY / "infra" / "jetson" / "veotrex-edge-ctl.sh"
UNIT_SRC = REPOSITORY / "infra" / "jetson" / "veotrex-edge.service"
MISSING = "Command {target} is not executable: No such file or directory"

STUBS = {
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >>"$SHIM_LOG"
case "$1" in
  --version) echo "systemd 255 (stub)";;
  daemon-reload)
    if [ -n "${SHIM_FAIL_DAEMON_RELOAD:-}" ]; then
      echo "Failed to reload daemon: stub" >&2; exit 1
    fi;;
  enable) touch "$SHIM_STATE/enabled";;
  disable) rm -f "$SHIM_STATE/enabled";;
  is-enabled)
    if [ -e "$SHIM_STATE/enabled" ]; then echo enabled; else echo disabled; exit 1; fi;;
  is-active)
    if [ -e "$SHIM_STATE/active" ]; then echo active; else echo inactive; exit 3; fi;;
  start|restart)
    if [ -z "${SHIM_FAIL_START:-}" ]; then touch "$SHIM_STATE/active"
    else rm -f "$SHIM_STATE/active"; fi;;
  stop) rm -f "$SHIM_STATE/active";;
  show)
    props=""; value=no
    for a in "${@:2}"; do
      case "$a" in
        -p) ;;
        --value) value=yes;;
        -p*) props=${a#-p};;
        *) if [ -z "$props" ]; then props=$a; fi;;
      esac
    done
    IFS=, read -ra names <<<"$props"
    for n in "${names[@]}"; do
      case "$n" in
        ActiveState)
          if [ -e "$SHIM_STATE/active" ]; then v=active; else v=failed; fi;;
        StatusText)
          if [ -e "$SHIM_STATE/active" ]; then
            v=${SHIM_STATUS:-ready commit=stub worker=READY}
          else v=""; fi;;
        UnitFileState)
          if [ -e "$SHIM_STATE/enabled" ]; then v=enabled; else v=disabled; fi;;
        WatchdogUSec)
          if [ -e "$SHIM_STATE/active" ]; then v=1min; else v=infinity; fi;;
        *) v=0;;
      esac
      if [ $value = yes ]; then echo "$v"; else echo "$n=$v"; fi
    done;;
esac
exit 0
""",
    "systemd-analyze": r"""#!/usr/bin/env bash
echo "systemd-analyze $*" >>"$SHIM_LOG"
[ "$1" = verify ] || exit 0
unit=$2
if [ -n "${SHIM_VERIFY_FAIL:-}" ]; then
  echo "veotrex-edge.service: injected verification failure"; exit 1
fi
target=$(sed -n 's/^ExecStart=//p' "$unit")
if [ ! -x "$VEOTREX_EDGE_ROOT$target" ]; then
  echo "veotrex-edge.service: Command $target is not executable: No such file or directory"
  exit 1
fi
echo "other.service: Standard output type syslog is obsolete (unrelated host warning)"
exit 0
""",
    "useradd": '#!/usr/bin/env bash\necho "useradd $*" >>"$SHIM_LOG"; touch "$SHIM_STATE/user"\n',
    "usermod": '#!/usr/bin/env bash\necho "usermod $*" >>"$SHIM_LOG"\n',
    "groupadd": (
        '#!/usr/bin/env bash\necho "groupadd $*" >>"$SHIM_LOG"; touch "$SHIM_STATE/group"\n'
    ),
    "getent": r"""#!/usr/bin/env bash
if [ "$1" = group ] && [ "$2" = veotrex-edge ]; then
  if [ -e "$SHIM_STATE/group" ]; then echo "veotrex-edge:x:979:"; exit 0; else exit 2; fi
fi
exec /usr/bin/getent "$@"
""",
    "id": r"""#!/usr/bin/env bash
if [ "${1:-}" = veotrex-edge ]; then
  if [ -e "$SHIM_STATE/user" ]; then
    groups="979(veotrex-edge),44(video),993(render)"
    echo "uid=996(veotrex-edge) gid=979(veotrex-edge) groups=$groups"
    exit 0
  fi
  exit 1
fi
exec /usr/bin/id "$@"
""",
    "journalctl": "#!/usr/bin/env bash\nexit 0\n",
}


class Host:
    """A temporary Jetson: prefix on disk, stub commands, recorded invocations."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "shim-bin"
        self.state = root / "shim-state"
        self.log = root / "shim.log"
        self.bin.mkdir()
        self.state.mkdir()
        self.log.touch()
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.prefix = root / "prefix"
        self.prefix.mkdir()
        self.env_extra: dict[str, str] = {}

    @property
    def unit(self) -> Path:
        return self.prefix / "etc/systemd/system/veotrex-edge.service"

    @property
    def config(self) -> Path:
        return self.prefix / "etc/veotrex-edge/edge.env"

    @property
    def current(self) -> Path:
        return self.prefix / "opt/veotrex-edge/current"

    @property
    def releases(self) -> Path:
        return self.prefix / "opt/veotrex-edge/releases"

    def run(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "VEOTREX_EDGE_ROOT": str(self.prefix),
            "VEOTREX_EDGE_CTL_TEST_MODE": "1",
            "VEOTREX_EDGE_HEALTH_TIMEOUT": "3",
            "VEOTREX_EDGE_HEALTH_POLL": "1",
            "SHIM_LOG": str(self.log),
            "SHIM_STATE": str(self.state),
            **self.env_extra,
        }
        env.pop("SUDO_USER", None)
        result = subprocess.run(  # noqa: S603 - repository script under test
            [str(CTL), *args], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == expect, (args, result.stdout, result.stderr)
        return result

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def enabled(self) -> bool:
        return (self.state / "enabled").exists()

    def make_release(
        self, sha: str, *, executable: bool = True, provenance: str | None = None
    ) -> Path:
        dest = self.releases / sha
        (dest / "venv/bin").mkdir(parents=True)
        agent = dest / "venv/bin/veotrex-edge-agent"
        agent.write_text("#!/bin/sh\nexit 0\n")
        if executable:
            agent.chmod(0o755)
        (dest / "release.env").write_text(
            f"VEOTREX_EDGE_SOURCE_COMMIT={provenance or sha}\nVEOTREX_EDGE_VERSION=0.1.0+t\n"
        )
        return dest


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def operator() -> str:
    return (
        os.environ.get("USER")
        or subprocess.run(
            ["/usr/bin/id", "-un"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------ fresh install


def test_fresh_install_converges_without_enabling_or_starting(host: Host) -> None:
    result = host.run("install", "--operator", operator())
    out = result.stdout
    assert "created system user veotrex-edge" in out
    assert "config created" in out and "unit installed" in out
    assert "pre-release: ExecStart target" in out and "intentionally absent" in out
    assert "host infrastructure installed" in out
    assert "release not yet activated" in out
    assert "service intentionally disabled and inactive until 'activate <sha>'" in out
    calls = host.calls()
    assert "systemctl daemon-reload" in calls
    assert not any(c.startswith("systemctl enable") for c in calls), "install must never enable"
    assert not any(c.startswith(("systemctl start", "systemctl restart")) for c in calls)
    assert "usermod -G video,render veotrex-edge" in calls
    assert not host.enabled() and not host.current.exists()
    assert host.unit.read_bytes() == UNIT_SRC.read_bytes()
    assert stat.S_IMODE(host.config.stat().st_mode) == 0o640
    node = [line for line in host.config.read_text().splitlines() if "NODE_ID" in line]
    assert len(node) == 1 and not node[0].endswith("00000000-0000-0000-0000-000000000000")
    assert host.run("install", "--start", "--operator", operator(), expect=1).stderr


def test_reinstall_on_partial_state_preserves_config_and_identity(host: Host) -> None:
    """The exact state the real Jetson was left in: account, groups, directories, config and
    unit present, no current release, service disabled and inactive."""
    host.run("install", "--operator", operator())
    before = sha256(host.config)
    before_mode = stat.S_IMODE(host.config.stat().st_mode)
    host.log.write_text("")

    result = host.run("install", "--operator", operator())
    assert sha256(host.config) == before, "config must be preserved byte for byte"
    assert stat.S_IMODE(host.config.stat().st_mode) == before_mode
    assert "config preserved" in result.stdout and "service user exists" in result.stdout
    assert "unit unchanged" in result.stdout
    calls = host.calls()
    assert not any(c.startswith("useradd") for c in calls), "account must be reused"
    assert "usermod -G video,render veotrex-edge" in calls
    assert not any("sudo" in c or "docker" in c for c in calls)
    assert "systemctl daemon-reload" in calls
    assert not any(c.startswith("systemctl enable") for c in calls)
    assert not host.enabled() and not host.current.exists()
    assert "release not yet activated" in result.stdout


def test_reinstall_preserves_a_synthetic_existing_config(host: Host) -> None:
    host.config.parent.mkdir(parents=True)
    synthetic = (
        "VEOTREX_EDGE_NODE_ID=12345678-1234-4123-8123-123456789abc\n"
        "VEOTREX_EDGE_ENVIRONMENT=production\n"
        "VEOTREX_EDGE_LOG_LEVEL=INFO\n"
        "VEOTREX_EDGE_HEARTBEAT_INTERVAL_SECONDS=45\n"
    )
    host.config.write_text(synthetic)
    digest = sha256(host.config)
    result = host.run("install", "--operator", operator())
    assert sha256(host.config) == digest
    assert host.config.read_text() == synthetic
    assert "config preserved" in result.stdout
    assert "12345678" not in result.stdout, "config values are never printed"


def test_install_converges_a_wrongly_enabled_unit_when_no_release_exists(host: Host) -> None:
    (host.state / "enabled").touch()
    result = host.run("install", "--operator", operator())
    assert (
        "disabled veotrex-edge.service: it was enabled with no release to execute" in result.stdout
    )
    assert not host.enabled()


def test_install_stops_at_daemon_reload_failure_but_leaves_state_convergent(host: Host) -> None:
    host.run("install", "--operator", operator())
    digest = sha256(host.config)
    host.env_extra["SHIM_FAIL_DAEMON_RELOAD"] = "1"
    result = host.run("install", "--operator", operator(), expect=1)
    assert "daemon-reload failed" in result.stderr
    assert sha256(host.config) == digest and host.unit.exists()
    del host.env_extra["SHIM_FAIL_DAEMON_RELOAD"]
    host.run("install", "--operator", operator())


def test_install_reports_a_genuine_unit_problem_even_before_a_release(host: Host) -> None:
    host.env_extra["SHIM_VERIFY_FAIL"] = "1"
    result = host.run("install", "--operator", operator(), expect=1)
    assert "injected verification failure" in result.stderr
    assert "installed unit failed verification" in result.stderr
    assert not any(c == "systemctl daemon-reload" for c in host.calls())


# ---------------------------------------------------------------------- first activation


def test_first_activation_enables_only_after_strict_verify(host: Host) -> None:
    host.run("install", "--operator", operator())
    host.make_release("a" * 40)
    host.log.write_text("")
    result = host.run("activate", "a" * 40)
    calls = host.calls()
    verify_index = next(i for i, c in enumerate(calls) if c.startswith("systemd-analyze verify"))
    reload_index = calls.index("systemctl daemon-reload")
    enable_index = calls.index("systemctl enable veotrex-edge.service")
    assert verify_index < reload_index < enable_index
    assert "unit verified (strict)" in result.stdout
    assert "enabled for boot: enabled" in result.stdout
    assert host.enabled()
    assert os.readlink(host.current) == str(host.releases / ("a" * 40))
    assert not any(c.startswith(("systemctl start", "systemctl restart")) for c in calls)
    assert "activated without restart" in result.stdout


def test_first_activation_with_restart_verifies_health(host: Host) -> None:
    host.run("install", "--operator", operator())
    host.make_release("a" * 40)
    result = host.run("activate", "a" * 40, "--restart")
    assert "systemctl restart veotrex-edge.service" in host.calls()
    assert "healthy: ready" in result.stdout
    host.env_extra["SHIM_STATUS"] = "degraded commit=stub worker=FAILED"
    assert host.run("activate", "a" * 40, "--restart", expect=2).stdout.count("DEGRADED") == 1


def test_failed_first_activation_removes_current_and_never_enables(host: Host) -> None:
    host.run("install", "--operator", operator())
    host.make_release("b" * 40)
    host.env_extra["SHIM_VERIFY_FAIL"] = "1"
    result = host.run("activate", "b" * 40, expect=1)
    assert "removed" in result.stdout and "nothing was enabled" in result.stdout
    assert not host.current.exists() and not host.enabled()
    assert not any(c.startswith("systemctl enable") for c in host.calls())


def test_activation_refuses_incomplete_or_mismatched_releases(host: Host) -> None:
    host.run("install", "--operator", operator())
    assert "does not exist" in host.run("activate", "c" * 40, expect=1).stderr
    host.make_release("c" * 40, executable=False)
    assert "no executable agent" in host.run("activate", "c" * 40, expect=1).stderr
    host.make_release("d" * 40, provenance="e" * 40)
    assert "does not name" in host.run("activate", "d" * 40, expect=1).stderr
    assert not host.current.exists() and not host.enabled()


# ------------------------------------------------------------------------ update/rollback


def test_update_and_rollback_keep_boot_enablement_and_a_failed_b_never_destroys_a(
    host: Host,
) -> None:
    host.run("install", "--operator", operator())
    a, b = "a" * 40, "b" * 40
    host.make_release(a)
    host.make_release(b)
    host.run("activate", a, "--restart")
    assert os.readlink(host.current).endswith(a) and host.enabled()

    # A broken B must leave A current and enabled.
    host.env_extra["SHIM_VERIFY_FAIL"] = "1"
    result = host.run("activate", b, "--restart", expect=1)
    assert f"restored current -> {host.releases / a}" in result.stdout
    assert os.readlink(host.current).endswith(a) and host.enabled()
    assert not any(c.startswith("systemctl restart") for c in host.calls()[-3:])
    del host.env_extra["SHIM_VERIFY_FAIL"]

    host.run("activate", b, "--restart")
    assert os.readlink(host.current).endswith(b)
    assert os.readlink(host.prefix / "opt/veotrex-edge/previous").endswith(a)
    host.run("rollback")
    assert os.readlink(host.current).endswith(a) and host.enabled()
    assert host.run("status").stdout.count("current:") == 1


# --------------------------------------------------------------------- preflight/status


def test_preflight_distinguishes_absent_visible_and_protected_config(host: Host) -> None:
    out = host.run("preflight").stdout
    assert "config: absent" in out
    host.run("install", "--operator", operator())
    out = host.run("preflight").stdout
    assert "config: -rw-r-----" in out and "edge.env" in out
    assert "NODE_ID" not in out
    if os.geteuid() == 0:  # pragma: no cover - root can always search
        return
    host.config.parent.chmod(0o000)
    try:
        out = host.run("preflight").stdout
        assert "config: protected (presence not observable as" in out
        assert "not installed" not in out
        status = host.run("status").stdout
        assert "config:   protected" in status
    finally:
        host.config.parent.chmod(0o750)


def test_preflight_enabled_line_is_a_single_state(host: Host) -> None:
    out = host.run("preflight").stdout
    lines = [line for line in out.splitlines() if line.startswith("veotrex-edge-ctl: enabled:")]
    assert lines == ["veotrex-edge-ctl: enabled: disabled"]
    assert "\nno\n" not in out and "\ninactive\n" not in out


def test_test_mode_refuses_root_semantics_in_script_text() -> None:
    text = CTL.read_text()
    assert "VEOTREX_EDGE_CTL_TEST_MODE is for unprivileged tests only, never root" in text
