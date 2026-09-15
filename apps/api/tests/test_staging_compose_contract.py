"""The staging deployment must not silently inherit a placeholder authority. No Docker needed.

`Settings` gives oidc_issuer and oidc_audience defaults under *.example.invalid so the test suite
can construct settings without a tenant. A deployment that leaves them unset does not fail loudly:
the API starts, serves unauthenticated routes, and verifies every bearer token against an issuer
that does not exist. That shipped to the Hostinger VPS - the API was healthy while no access token
from the real tenant could ever have been accepted.

The rule below is deliberately general: any Settings default pointing at a placeholder authority
must be wired explicitly in the staging compose, and wired as REQUIRED. It therefore also covers
placeholder-defaulted fields added later, not just these two.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
CONFIG = REPOSITORY / "apps" / "api" / "src" / "veotrex_api" / "config.py"
HOSTINGER = REPOSITORY / "infra" / "staging" / "hostinger"
COMPOSE_PATH = HOSTINGER / "compose.yaml"
ENV_EXAMPLE = HOSTINGER / "hostinger.env.example"

PLACEHOLDER_AUTHORITY = re.compile(
    r"^\s*(\w+):\s*str\s*=\s*\"[^\"]*\.invalid[^\"]*\"", re.MULTILINE
)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text())
    return loaded


def _placeholder_defaulted_fields() -> list[str]:
    fields = PLACEHOLDER_AUTHORITY.findall(CONFIG.read_text())
    assert fields, "expected at least one placeholder-defaulted setting to guard"
    return fields


def _api_environment(compose: dict[str, Any]) -> dict[str, str]:
    environment: dict[str, str] = compose["services"]["api"]["environment"]
    return environment


def test_every_placeholder_defaulted_setting_is_wired_for_staging(compose: dict[str, Any]) -> None:
    """A default of *.example.invalid must never be what a deployed API actually uses."""
    environment = _api_environment(compose)
    for field in _placeholder_defaulted_fields():
        variable = f"VEOTREX_{field.upper()}"
        assert variable in environment, (
            f"{field} defaults to a placeholder authority and is not set in the staging compose; "
            "the API would start and verify tokens against an issuer that does not exist"
        )


def test_those_settings_fail_closed_rather_than_defaulting(compose: dict[str, Any]) -> None:
    """`${VAR:?...}` refuses to start; `${VAR:-default}` would reintroduce the silent failure."""
    environment = _api_environment(compose)
    for field in _placeholder_defaulted_fields():
        value = environment[f"VEOTREX_{field.upper()}"]
        assert value.startswith("${") and ":?" in value, (
            f"VEOTREX_{field.upper()} must be mandatory interpolation, got {value!r}"
        )


def _active_lines(text: str) -> list[str]:
    """Executable lines only: a comment explaining a hazard is not an occurrence of it."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_no_placeholder_authority_is_committed_as_a_staging_value() -> None:
    """The example file documents the shape; it must not ship a usable-looking fake authority."""
    for path in (COMPOSE_PATH, ENV_EXAMPLE):
        offenders = [ln for ln in _active_lines(path.read_text()) if ".example.invalid" in ln]
        assert not offenders, f"{path.name}: {offenders}"


def test_the_example_env_documents_each_required_variable() -> None:
    example = ENV_EXAMPLE.read_text()
    for field in _placeholder_defaulted_fields():
        assert f"VEOTREX_{field.upper()}=" in example, field


def test_issuer_example_satisfies_the_settings_validator() -> None:
    """issuer_must_be_absolute_https_url: absolute HTTPS, trailing slash. A tenant host is easy
    to paste without the slash, which fails only at container start."""
    match = re.search(r"^VEOTREX_OIDC_ISSUER=(\S+)", ENV_EXAMPLE.read_text(), re.MULTILINE)
    assert match, "the example must carry an issuer line to copy"
    issuer = match.group(1)
    assert issuer.startswith("https://") and issuer.endswith("/"), issuer
