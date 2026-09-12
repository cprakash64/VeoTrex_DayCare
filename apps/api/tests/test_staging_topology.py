"""Static security properties of the staging control plane. No Docker, no network, no deploy.

These tests make the deployment's guarantees enforced rather than merely documented: they fail
if PostgreSQL is ever published, if a service other than the proxy binds a public port, if a
secret value is committed, if the migration stops being a discrete step, or if the edge stack
leaks into the CPU-only control-plane images.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
STAGING = REPOSITORY / "infra" / "staging"
COMPOSE_PATH = STAGING / "compose.yaml"
CADDYFILE_PATH = STAGING / "Caddyfile"
DOCKERIGNORE_PATH = REPOSITORY / ".dockerignore"
API_DOCKERFILE = STAGING / "Dockerfile.api"
WEB_DOCKERFILE = STAGING / "Dockerfile.web"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text())
    return loaded


def _service(compose: dict[str, Any], name: str) -> dict[str, Any]:
    service: dict[str, Any] = compose["services"][name]
    return service


def _uncommented(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------- public exposure
def test_only_the_proxy_publishes_ports(compose: dict[str, Any]) -> None:
    publishing = {
        name: service.get("ports")
        for name, service in compose["services"].items()
        if service.get("ports")
    }
    assert set(publishing) == {"proxy"}, f"unexpected published ports: {publishing}"
    assert sorted(str(p) for p in publishing["proxy"]) == ["443:443", "443:443/udp", "80:80"]


def test_postgres_has_no_published_port_at_all(compose: dict[str, Any]) -> None:
    postgres = _service(compose, "postgres")
    # Not "empty ports" - the key must be absent entirely.
    assert "ports" not in postgres
    assert "5432:5432" not in COMPOSE_PATH.read_text()


def test_database_network_is_internal(compose: dict[str, Any]) -> None:
    assert compose["networks"]["data"]["internal"] is True
    assert _service(compose, "postgres")["networks"] == ["data"]
    # The proxy must never reach the database directly.
    assert "data" not in _service(compose, "proxy")["networks"]


def test_web_and_api_are_reachable_only_through_the_proxy(compose: dict[str, Any]) -> None:
    for name in ("web", "api"):
        service = _service(compose, name)
        assert "ports" not in service, f"{name} must not publish a port"
        assert service.get("expose")


# --------------------------------------------------------------------- durability
def test_staging_database_is_durable_not_disposable(compose: dict[str, Any]) -> None:
    postgres = _service(compose, "postgres")
    assert "tmpfs" not in postgres, "staging must not use the disposable test-cluster storage"
    assert postgres["volumes"] == ["veotrex-staging-postgres:/var/lib/postgresql/data"]
    assert postgres["restart"] == "unless-stopped"
    assert postgres["healthcheck"]
    # Must not reuse the local development volume.
    assert "veotrex-postgres:" not in str(postgres)


# --------------------------------------------------------------------- secrets
def test_no_secret_value_is_committed_in_compose(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        environment = service.get("environment", {}) or {}
        assert "POSTGRES_PASSWORD" not in environment, f"{name} carries a literal password"
        for key, value in environment.items():
            if key.endswith("_REF"):
                assert str(value).startswith(("file:", "env:")), f"{key} is not a reference"
    for definition in compose["secrets"].values():
        # Files outside the repository, supplied by the operator.
        assert "${VEOTREX_STAGING_SECRETS_DIR" in definition["file"]


def test_high_value_secrets_are_file_references_not_environment_values(
    compose: dict[str, Any],
) -> None:
    api = _service(compose, "api")
    environment = api["environment"]
    assert environment["VEOTREX_VAULT_MASTER_KEY_REF"] == "file:/run/secrets/vault_master_key"
    assert (
        environment["VEOTREX_RING_CLIENT_SECRET_REF"] == "file:/run/secrets/ring_client_secret"  # noqa: S105 - a mount path, not a secret
    )
    assert (
        environment["VEOTREX_RING_HMAC_SIGNING_KEY_REF"]
        == "file:/run/secrets/ring_hmac_signing_key"
    )
    assert set(api["secrets"]) == {
        "vault_master_key",
        "ring_client_secret",
        "ring_hmac_signing_key",
    }


def test_public_origin_is_configured_never_derived_from_headers(compose: dict[str, Any]) -> None:
    api = _service(compose, "api")
    assert api["environment"]["VEOTREX_PUBLIC_ORIGIN"].startswith("https://")
    assert api["environment"]["VEOTREX_ENVIRONMENT"] == "staging"


# --------------------------------------------------------------------- migration
def test_migration_is_a_discrete_job_that_replicas_cannot_race(compose: dict[str, Any]) -> None:
    migrate = _service(compose, "migrate")
    assert migrate["profiles"] == ["migrate"], "migration must not run on an ordinary `up`"
    assert migrate["command"] == ["alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"]
    assert migrate["restart"] == "no"
    # The API itself must never run migrations at startup.
    assert "alembic" not in str(_service(compose, "api").get("command", ""))


# --------------------------------------------------------------------- reverse proxy
def test_proxy_routes_the_single_origin_to_both_applications() -> None:
    caddy = CADDYFILE_PATH.read_text()
    assert "path /v1/*" in caddy
    assert "reverse_proxy api:8000" in caddy
    assert "reverse_proxy web:3000" in caddy


def test_proxy_normalises_forwarding_headers() -> None:
    caddy = _uncommented(CADDYFILE_PATH.read_text())
    joined = "\n".join(caddy)
    assert "request_header X-Forwarded-Host {host}" in joined
    assert "request_header X-Forwarded-Proto https" in joined
    assert "request_header -Forwarded" in joined


def test_proxy_bounds_request_bodies_and_hides_health_endpoints() -> None:
    caddy = CADDYFILE_PATH.read_text()
    assert "max_size" in caddy
    # /health/ready touches the database and both endpoints echo version/environment.
    assert "respond @health 404" in caddy
    assert "admin off" in caddy


# --------------------------------------------------------------------- images
def test_build_context_excludes_appledouble_sidecars() -> None:
    ignored = _uncommented(DOCKERIGNORE_PATH.read_text())
    assert "._*" in ignored, "AppleDouble files break Alembic's *.py discovery"
    assert ".env" in ignored
    assert "infra/local/secrets/" in ignored


def test_control_plane_images_are_cpu_only_and_non_root() -> None:
    for dockerfile in (API_DOCKERFILE, WEB_DOCKERFILE):
        text = dockerfile.read_text()
        # Instructions only: a comment explaining that CUDA is excluded is not an inclusion.
        instructions = "\n".join(_uncommented(text)).lower()
        for forbidden in ("cuda", "tensorrt", "gstreamer", "nvidia", "deepstream", ".engine"):
            assert forbidden not in instructions, (
                f"{dockerfile.name} pulls in edge-only {forbidden}"
            )
        assert re.search(r"^USER veotrex$", text, re.M), f"{dockerfile.name} runs as root"
        assert "HEALTHCHECK" in text


def test_api_image_does_not_run_a_development_server() -> None:
    api = "\n".join(_uncommented(API_DOCKERFILE.read_text()))
    assert "--reload" not in api
    assert "--workers" in api
    web = "\n".join(_uncommented(WEB_DOCKERFILE.read_text()))
    assert "next dev" not in web
    assert "NODE_ENV=production" in web
