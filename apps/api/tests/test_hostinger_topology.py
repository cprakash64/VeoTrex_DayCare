"""Static safety properties of the Hostinger deployment. No Docker, no network, no live host.

The Hostinger VPS already runs five production sites behind its own nginx. These tests fail if
the deployment ever gains a competing proxy, publishes a port beyond loopback, exposes the
database, loses outbound reachability, or breaks the nginx path semantics Ring depends on.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
HOSTINGER = REPOSITORY / "infra" / "staging" / "hostinger"
COMPOSE_PATH = HOSTINGER / "compose.yaml"
HTTP_CONF = HOSTINGER / "nginx" / "daycare-http.conf"
HTTPS_CONF = HOSTINGER / "nginx" / "daycare-https.conf"
RUNBOOK = HOSTINGER / "README.md"
GENERIC_COMPOSE = REPOSITORY / "infra" / "staging" / "compose.yaml"

HOSTNAME = "daycare.veotrex.com"
WEB_BIND = "127.0.0.1:3100:3000"
API_BIND = "127.0.0.1:8100:8000"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text())
    return loaded


def _directives(text: str) -> list[str]:
    """nginx directive lines only; a comment describing a hazard is not the hazard."""
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------- no competing ingress
def test_hostinger_stack_contains_no_proxy_service(compose: dict[str, Any]) -> None:
    """nginx owns :80/:443 on this host; a second ingress would take five live sites down."""
    assert set(compose["services"]) == {
        "postgres",
        "migrate",
        "runtime-role",
        "ring-pending-expiry",
        "api",
        "web",
    }
    rendered = COMPOSE_PATH.read_text().lower()
    for forbidden in ("caddy", "traefik", "haproxy"):
        assert forbidden not in "\n".join(_directives(rendered))


def test_no_service_publishes_a_public_web_port(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            # "0.0.0.0" appears here only to be asserted against, never bound.
            wildcard = ("80:", "443:", "0.0.0.0")  # noqa: S104 - asserted against
            assert not str(port).startswith(wildcard), f"{name} exposes {port}"
            assert ":80:" not in str(port) and ":443:" not in str(port)


def test_generic_staging_stack_is_preserved_unchanged() -> None:
    """The empty-host Caddy stack still exists; the Hostinger variant is additive."""
    generic: dict[str, Any] = yaml.safe_load(GENERIC_COMPOSE.read_text())
    assert "proxy" in generic["services"]


# --------------------------------------------------------------------- exact loopback bindings
def test_web_and_api_bind_only_to_loopback(compose: dict[str, Any]) -> None:
    assert compose["services"]["web"]["ports"] == [WEB_BIND]
    assert compose["services"]["api"]["ports"] == [API_BIND]


def test_database_and_migration_publish_nothing(compose: dict[str, Any]) -> None:
    # The host already runs its own PostgreSQL on 127.0.0.1:5432.
    assert "ports" not in compose["services"]["postgres"]
    assert "ports" not in compose["services"]["migrate"]
    assert "5432:5432" not in COMPOSE_PATH.read_text()


# --------------------------------------------------------------------- network reachability
def test_outbound_capable_network_is_not_internal(compose: dict[str, Any]) -> None:
    """The API calls Ring's OAuth/device APIs and web calls Auth0; `app` must reach the Internet."""
    networks = compose["networks"]
    assert networks["data"]["internal"] is True
    assert not (networks.get("app") or {}).get("internal", False)
    assert set(compose["services"]["api"]["networks"]) == {"app", "data"}
    assert set(compose["services"]["web"]["networks"]) == {"app", "data"}
    # The database is reachable only on the private network.
    assert compose["services"]["postgres"]["networks"] == ["data"]


# --------------------------------------------------------------------- persistence and secrets
def test_database_is_durable_and_namespaced(compose: dict[str, Any]) -> None:
    postgres = compose["services"]["postgres"]
    assert "tmpfs" not in postgres
    assert postgres["volumes"] == ["veotrex-daycare-postgres:/var/lib/postgresql/data"]
    assert postgres["restart"] == "unless-stopped"
    assert postgres["healthcheck"]
    assert compose["name"] == "veotrex-daycare"
    # Must not reuse another project's volume on this shared host.
    directives = "\n".join(_directives(COMPOSE_PATH.read_text()))
    for foreign in ("xpertapply_postgres_data", "luna-ai-v1_", "veotrex-staging-postgres"):
        assert foreign not in directives


def test_every_secret_is_a_reference_not_a_value(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        environment = service.get("environment", {}) or {}
        assert "POSTGRES_PASSWORD" not in environment, f"{name} carries a literal password"
        for key, value in environment.items():
            if key.endswith("_REF"):
                assert str(value).startswith("file:/run/secrets/"), f"{key} is not a file mount"
    for definition in compose["secrets"].values():
        assert "${VEOTREX_HOSTINGER_SECRETS_DIR" in definition["file"]


def test_database_url_arrives_by_reference_not_environment(compose: dict[str, Any]) -> None:
    """Every DSN is a file-mounted secret reference, and the identities are split (V1-00A):
    migration and role provisioning use the admin DSN; the API mounts ONLY the runtime DSN."""
    for service in ("migrate", "runtime-role", "ring-pending-expiry"):
        environment = compose["services"][service]["environment"]
        assert environment["VEOTREX_DATABASE_URL_REF"] == "file:/run/secrets/database_url"
        assert "VEOTREX_DATABASE_URL" not in environment
        assert "database_url" in compose["services"][service]["secrets"]
    api = compose["services"]["api"]
    assert api["environment"]["VEOTREX_DATABASE_URL_REF"] == "file:/run/secrets/api_database_url"
    assert "VEOTREX_DATABASE_URL" not in api["environment"]
    assert "api_database_url" in api["secrets"]
    assert "database_url" not in api["secrets"], "the API must never mount the admin DSN"
    assert "api_database_password" not in api["secrets"]
    assert "postgres_password" not in api["secrets"]


def test_runtime_role_job_is_a_discrete_idempotent_profile(compose: dict[str, Any]) -> None:
    job = compose["services"]["runtime-role"]
    assert job["profiles"] == ["runtime-role"]
    assert job["restart"] == "no"
    assert job["command"][:2] == ["veotrex-db-runtime-role", "apply"]
    assert "--password-ref" in job["command"]
    assert "file:/run/secrets/api_database_password" in job["command"]
    assert not any(token.startswith("postgresql") for token in job["command"])
    assert "api_database_password" in job["secrets"]
    assert "ports" not in job
    assert job["networks"] == ["data"]


def test_pending_expiry_job_is_a_read_only_by_default_maintenance_profile(
    compose: dict[str, Any],
) -> None:
    """V1-00A-PROD-R2: the janitor is a discrete admin-identity job whose default invocation
    is the dry run. It deletes ciphertext rows and never decrypts, so it must not hold the
    vault master key, and it never needs the runtime role's password."""
    job = compose["services"]["ring-pending-expiry"]
    assert job["profiles"] == ["maintenance"]
    assert job["restart"] == "no"
    assert job["command"][:2] == ["veotrex-ring-pending-expiry", "dry-run"]
    assert "--limit" in job["command"]
    assert "apply" not in job["command"]
    assert not any(token.startswith("postgresql") for token in job["command"])
    assert job["secrets"] == ["database_url"]
    assert "VEOTREX_VAULT_MASTER_KEY_REF" not in job["environment"]
    assert "ports" not in job
    assert job["networks"] == ["data"]


def test_migration_remains_a_discrete_job(compose: dict[str, Any]) -> None:
    migrate = compose["services"]["migrate"]
    assert migrate["profiles"] == ["migrate"]
    assert migrate["command"] == ["alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"]
    assert migrate["restart"] == "no"
    assert "alembic" not in str(compose["services"]["api"].get("command", ""))


# --------------------------------------------------------------------- nginx templates
@pytest.mark.parametrize("path", [HTTP_CONF, HTTPS_CONF])
def test_nginx_template_is_a_single_named_host(path: Path) -> None:
    directives = "\n".join(_directives(path.read_text()))
    assert f"server_name {HOSTNAME};" in directives
    # Either would capture traffic belonging to the five existing sites.
    assert "default_server" not in directives
    assert not re.search(r"server_name\s+[_*]", directives)
    assert "veotrex.com" in directives and "spendwize" not in directives


@pytest.mark.parametrize("path", [HTTP_CONF, HTTPS_CONF])
def test_proxy_pass_preserves_the_uri_prefix(path: Path) -> None:
    """A trailing slash on proxy_pass strips /v1 and would 404 every Ring callback."""
    directives = "\n".join(_directives(path.read_text()))
    assert "proxy_pass http://127.0.0.1:8100;" in directives
    assert "proxy_pass http://127.0.0.1:3100;" in directives
    assert "proxy_pass http://127.0.0.1:8100/" not in directives
    assert "proxy_pass http://127.0.0.1:3100/" not in directives
    assert "location /v1/ {" in directives


@pytest.mark.parametrize("path", [HTTP_CONF, HTTPS_CONF])
def test_no_websocket_directives_are_present(path: Path) -> None:
    """Verified in source: no WebSocket, SSE or socket.io anywhere in the application."""
    directives = "\n".join(_directives(path.read_text())).lower()
    assert "upgrade" not in directives
    assert "connection" not in directives


@pytest.mark.parametrize("path", [HTTP_CONF, HTTPS_CONF])
def test_health_endpoints_are_not_publicly_routed(path: Path) -> None:
    directives = "\n".join(_directives(path.read_text()))
    assert "location ^~ /health/" in directives
    assert "return 404;" in directives


@pytest.mark.parametrize("path", [HTTP_CONF, HTTPS_CONF])
def test_bounded_bodies_and_timeouts(path: Path) -> None:
    directives = "\n".join(_directives(path.read_text()))
    assert "client_max_body_size" in directives
    for timeout in ("proxy_connect_timeout", "proxy_send_timeout", "proxy_read_timeout"):
        assert timeout in directives


def test_hsts_only_on_the_https_template() -> None:
    """Advertising HSTS before a certificate exists would pin browsers to a dead endpoint."""
    assert "Strict-Transport-Security" not in "\n".join(_directives(HTTP_CONF.read_text()))
    https = "\n".join(_directives(HTTPS_CONF.read_text()))
    assert "Strict-Transport-Security" in https
    # preload is effectively irreversible; not appropriate for a staging hostname.
    assert "preload" not in https


def test_templates_reference_certificates_but_embed_no_key_material() -> None:
    for path in (HTTP_CONF, HTTPS_CONF):
        body = path.read_text()
        assert "BEGIN" not in body and "PRIVATE KEY" not in body
    https = HTTPS_CONF.read_text()
    assert f"/etc/letsencrypt/live/{HOSTNAME}/fullchain.pem" in https


def test_runbook_forbids_caddy_and_records_the_bindings() -> None:
    runbook = RUNBOOK.read_text()
    assert "DO NOT START CADDY" in runbook
    assert "127.0.0.1:3100" in runbook and "127.0.0.1:8100" in runbook
    assert "178.16.143.10" in runbook and HOSTNAME in runbook
    # The host's own database must be called out as untouchable.
    assert "127.0.0.1:5432" in runbook
