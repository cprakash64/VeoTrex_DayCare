"""Static contract of the Jetson supervision files (V1-00B). No systemd, no root, no host."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

from veotrex_edge_agent.main import EXIT_CONFIG

REPOSITORY = Path(__file__).resolve().parents[3]
JETSON = REPOSITORY / "infra" / "jetson"
UNIT = JETSON / "veotrex-edge.service"
CTL = JETSON / "veotrex-edge-ctl.sh"
HARNESS = JETSON / "user-scope-failure-injection.sh"
ENV_EXAMPLE = JETSON / "edge.env.example"
README = JETSON / "README.md"


def unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(UNIT.read_text())
    return parser


def test_service_owns_a_foreground_notify_process_as_a_dedicated_user() -> None:
    service = unit()["Service"]
    assert service["Type"] == "notify" and service["NotifyAccess"] == "main"
    assert service["User"] == "veotrex-edge" and service["Group"] == "veotrex-edge"
    assert set(service["SupplementaryGroups"].split()) == {"video", "render"}
    assert service["ExecStart"] == "/opt/veotrex-edge/current/venv/bin/veotrex-edge-agent"
    assert "sh " not in service["ExecStart"] and "$" not in service["ExecStart"]
    assert service["WorkingDirectory"] == "/var/lib/veotrex-edge"
    assert service["StateDirectory"] == "veotrex-edge"


def test_restart_and_crash_loop_semantics() -> None:
    parsed = unit()
    assert parsed["Unit"]["StartLimitIntervalSec"] == "600"
    assert parsed["Unit"]["StartLimitBurst"] == "5"
    service = parsed["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10s"
    assert service["RestartPreventExitStatus"] == str(EXIT_CONFIG)
    assert service["KillMode"] == "mixed" and service["KillSignal"] == "SIGTERM"
    assert service["TimeoutStopSec"] == "30s"
    assert service["WatchdogSec"] == "60s"
    assert "StartLimit" not in "".join(service.keys()), "StartLimit* belongs in [Unit]"


def test_boot_semantics_do_not_require_internet() -> None:
    after = unit()["Unit"]["After"].split()
    assert "network-online.target" not in after
    assert "local-fs.target" in after
    assert unit()["Install"]["WantedBy"] == "multi-user.target"


def test_no_secret_or_literal_environment_in_the_unit() -> None:
    text = UNIT.read_text()
    assert not re.search(r"^Environment=", text, re.M)
    env_files = re.findall(r"^EnvironmentFile=(.*)$", text, re.M)
    assert env_files == ["/opt/veotrex-edge/current/release.env", "/etc/veotrex-edge/edge.env"]
    assert not re.search(r"(?i)(token|secret|password|dsn)\s*=", text)


def test_hardening_is_explicit_and_gpu_safe() -> None:
    service = unit()["Service"]
    expected = {
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectControlGroups": "yes",
        "RestrictSUIDSGID": "yes",
        "LockPersonality": "yes",
        "RestrictNamespaces": "yes",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "UMask": "0077",
        "RemoveIPC": "yes",
        "SystemCallFilter": "@system-service",
        "ProtectProc": "invisible",
        "ProcSubset": "pid",
    }
    for key, value in expected.items():
        assert service[key] == value, key
    # Would break the TensorRT worker's /dev/nvmap and /dev/nvgpu access.
    assert "PrivateDevices" not in service
    assert "DevicePolicy" not in service
    assert "MemoryDenyWriteExecute" not in service


def test_env_example_holds_only_non_secret_edge_settings() -> None:
    keys = re.findall(r"^(VEOTREX_EDGE_[A-Z_]+)=", ENV_EXAMPLE.read_text(), re.M)
    assert keys == [
        "VEOTREX_EDGE_NODE_ID",
        "VEOTREX_EDGE_ENVIRONMENT",
        "VEOTREX_EDGE_LOG_LEVEL",
        "VEOTREX_EDGE_HEARTBEAT_INTERVAL_SECONDS",
    ]
    assert "VEOTREX_EDGE_NODE_ID=00000000-0000-0000-0000-000000000000" in ENV_EXAMPLE.read_text()


def test_control_script_deployment_rules() -> None:
    text = CTL.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "reset --hard" not in text
    assert "merge --ff-only" in text
    assert "--no-editable" in text and "--frozen" in text
    assert '--reinstall-package "$PACKAGE"' in text, "never ship a stale cached wheel"
    assert "useradd --system" in text and "/usr/sbin/nologin" in text
    assert 'usermod -G "$DEVICE_GROUPS"' in text and "DEVICE_GROUPS=video,render" in text
    assert "systemd-analyze verify" in text
    assert not re.search(r"^\s*docker\b|docker\.sock", text, re.M)
    assert "chown -R" not in text
    assert 'ln -sfn "$dest" "$CURRENT_LINK.tmp"; mv -T' in text, "atomic release switch"
    assert "kept existing $CONF_FILE" in text, "install must never overwrite config"
    assert "require_operator build" in text and "require_root install" in text
    for path in (UNIT, CTL, HARNESS, ENV_EXAMPLE, README):
        assert path.exists(), path
    assert CTL.stat().st_mode & 0o111, "control script must be executable"
    assert HARNESS.stat().st_mode & 0o111


def test_readme_documents_journal_and_rollback() -> None:
    text = README.read_text()
    for needle in (
        "journalctl -u veotrex-edge -b",
        "journalctl -u veotrex-edge -n 100",
        "journalctl -u veotrex-edge -p err --since",
        "veotrex-edge-ctl.sh rollback",
        "systemctl status veotrex-edge",
        "RestartPreventExitStatus",
    ):
        assert needle in text, needle
