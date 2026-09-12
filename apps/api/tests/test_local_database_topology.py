"""Static safety properties of the local database topology. No Docker, no PostgreSQL, no network.

These tests fail if the development database is ever republished on a wildcard address, if the
test cluster stops being a separate PostgreSQL server, if a database password returns to source
control, or if a workspace-narrowing uv invocation reappears in the project commands.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPOSITORY / "infra" / "local" / "compose.yaml"
MAKEFILE_PATH = REPOSITORY / "Makefile"
GITIGNORE_PATH = REPOSITORY / ".gitignore"
WORKFLOW_PATH = REPOSITORY / ".github" / "workflows" / "ci.yml"

LOOPBACK_PREFIX = "127.0.0.1:"
WILDCARD_PREFIX = "0.0.0.0"  # noqa: S104 - asserted against, never bound to


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text())
    return loaded


def _service(compose: dict[str, Any], name: str) -> dict[str, Any]:
    service: dict[str, Any] = compose["services"][name]
    return service


def _command_lines(text: str) -> list[str]:
    """Executable lines only: a comment explaining a hazard is not an occurrence of it."""
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------- binding
def test_every_published_database_port_binds_to_loopback(compose: dict[str, Any]) -> None:
    published = [
        port for service in compose["services"].values() for port in service.get("ports", [])
    ]
    assert published, "expected published ports to assert on"
    for port in published:
        # A bare "5432:5432" publishes on every interface and reaches the LAN. The bind address
        # is the boundary: Docker's published-port DNAT rules bypass host firewall filtering.
        assert str(port).startswith(LOOPBACK_PREFIX), f"{port} is not loopback-bound"
        assert not str(port).startswith(WILDCARD_PREFIX)
        assert not str(port).startswith("::")


def test_development_and_test_use_distinct_host_ports(compose: dict[str, Any]) -> None:
    development = str(_service(compose, "postgres")["ports"][0])
    test = str(_service(compose, "postgres-test")["ports"][0])
    assert development != test
    assert "5432" in development and "55433" in test


# --------------------------------------------------------------------- separate clusters
def test_test_cluster_shares_no_storage_with_development(compose: dict[str, Any]) -> None:
    development = _service(compose, "postgres")
    test = _service(compose, "postgres-test")
    assert development["volumes"] == ["veotrex-postgres:/var/lib/postgresql/data"]
    # The disposable cluster uses tmpfs and must never mount the development named volume.
    assert test.get("tmpfs")
    assert "volumes" not in test
    assert "veotrex-postgres" not in str(test)


def test_services_are_independently_selectable(compose: dict[str, Any]) -> None:
    # The test cluster sits behind a profile, so "docker compose up" cannot start it by accident.
    assert _service(compose, "postgres-test")["profiles"] == ["test"]
    assert "profiles" not in _service(compose, "postgres")
    assert _service(compose, "postgres")["environment"]["POSTGRES_DB"] == "veotrex"
    assert _service(compose, "postgres-test")["environment"]["POSTGRES_DB"] == "veotrex_test"


# --------------------------------------------------------------------- secrets
def test_no_database_password_is_committed_in_compose(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        environment = service.get("environment", {})
        assert "POSTGRES_PASSWORD" not in environment, f"{name} carries a literal password"
        assert environment["POSTGRES_PASSWORD_FILE"].startswith("/run/secrets/")
        assert service["secrets"]
    for definition in compose["secrets"].values():
        # An operator-created file; a missing file fails the service closed.
        assert definition["file"].startswith("./secrets/")


def test_local_secret_files_cannot_be_committed() -> None:
    assert "infra/local/secrets/" in GITIGNORE_PATH.read_text()


def test_continuous_integration_sources_its_password_from_a_secret() -> None:
    workflow = WORKFLOW_PATH.read_text()
    assert "${{ secrets.CI_POSTGRES_PASSWORD }}" in workflow
    assert "POSTGRES_DB: veotrex_test" in workflow


_CREDENTIAL_URL = re.compile(r"postgresql(?:\+psycopg)?://[A-Za-z0-9_]+:([^@/\s\"']+)@")
# Synthetic tokens used by existing unit tests. Deliberately short, obviously fake values.
_ALLOWED_PLACEHOLDERS = frozenset({"p", "pw", "placeholder", "password", "secret", "unused"})


def _tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present in every supported environment
        pytest.skip("git is unavailable")
    listing = subprocess.run(  # noqa: S603 - resolved executable, fixed argv, no shell
        [git, "ls-files", "-z"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return [REPOSITORY / name for name in listing.stdout.split("\0") if name]


def test_no_tracked_file_contains_a_real_database_password() -> None:
    """Every credential in a committed URL must be an obvious placeholder or a secret reference."""
    offenders: list[str] = []
    for path in _tracked_files():
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for secret in _CREDENTIAL_URL.findall(content):
            if secret.startswith("${{") or secret.startswith("REPLACE_WITH"):
                continue
            if secret.lower() not in _ALLOWED_PLACEHOLDERS:
                offenders.append(str(path.relative_to(REPOSITORY)))
    assert not offenders, f"non-placeholder database credentials in: {sorted(set(offenders))}"


def test_secret_scanning_does_not_allowlist_a_credential() -> None:
    """A pinned credential regex would both store the value and hide it from future scans."""
    # Active entries only: a comment explaining the decision is not an allowlist entry.
    active = _command_lines((REPOSITORY / ".gitleaks.toml").read_text())
    assert not any("regexes" in line for line in active)
    # compose is no longer path-exempt, so a re-added literal password is reported.
    assert not any("compose" in line for line in active)


# --------------------------------------------------------------------- workspace-safe commands
def test_database_commands_never_narrow_the_shared_workspace() -> None:
    """`uv run --package <member>` uninstalls the other member's dependencies."""
    for path in (MAKEFILE_PATH, WORKFLOW_PATH):
        for line in _command_lines(path.read_text()):
            assert "--package " not in line, f"{path.name} narrows the workspace: {line.strip()}"
    assert "uv run --all-packages" in MAKEFILE_PATH.read_text()


def test_make_targets_separate_development_and_test_lifecycles() -> None:
    makefile = MAKEFILE_PATH.read_text()
    for target in ("db-up:", "db-down:", "db-test-up:", "db-test-down:"):
        assert target in makefile
    for target in ("migrate:", "migrate-test:"):
        assert target in makefile
    # db-up names the development service explicitly so it cannot start the test cluster.
    assert "up -d --wait postgres\n" in makefile
    assert "--profile test up -d --wait postgres-test" in makefile
    # Both migration targets validate their destination before Alembic runs.
    assert "--require-development" in makefile
    assert "--require-test" in makefile
