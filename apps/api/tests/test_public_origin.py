"""Public HTTPS origin and deterministic Ring callback URL derivation."""

import pytest

from veotrex_api.config import Settings
from veotrex_api.public_origin import (
    ACCOUNT_LINK_PATH,
    DEFAULT_REDIRECT_PATH,
    TOKEN_EXCHANGE_PATH,
    WEBHOOK_PATH,
    InvalidPublicOrigin,
    callback_urls_for,
    validate_public_origin,
)

# Documentation/test hostnames only; no real VeoTrex deployment hostname exists yet.
VALID = "https://ring.example.test"


def test_valid_origin_is_canonicalised() -> None:
    origin = validate_public_origin(VALID)
    assert origin.origin == VALID
    assert origin.scheme == "https" and origin.port is None
    assert validate_public_origin("https://Ring.Example.Test/").origin == VALID
    assert validate_public_origin("https://ring.example.test:8443").port == 8443


@pytest.mark.parametrize(
    "raw",
    [
        "http://ring.example.test",  # scheme downgrade
        "ftp://ring.example.test",
        "//ring.example.test",
        "ring.example.test",
        "https://localhost",
        "https://localhost:8443",
        "https://api.localhost",
        "https://veotrex.local",
        "https://127.0.0.1",
        "https://10.1.2.3",
        "https://192.168.1.10",
        "https://169.254.169.254",
        "https://[::1]",
        "https://user:pass@ring.example.test",  # userinfo
        "https://token@ring.example.test",
        "https://ring.example.test/callback",  # path
        "https://ring.example.test/?a=b",  # query
        "https://ring.example.test#frag",  # fragment
        "https://*.example.test",  # wildcard
        "https://ring example.test",  # whitespace
        "https://-bad.example.test",
        "https://singlelabel",
        "https://",
        "",
        "https://" + "a" * 300 + ".test",
        None,
        12345,
    ],
)
def test_unsafe_or_malformed_origins_are_rejected(raw: object) -> None:
    with pytest.raises(InvalidPublicOrigin):
        validate_public_origin(raw)


def test_callback_urls_are_exact_and_constant() -> None:
    urls = callback_urls_for(VALID)
    assert urls == {
        "account_link_url": f"{VALID}{ACCOUNT_LINK_PATH}",
        "default_redirect_url": f"{VALID}{DEFAULT_REDIRECT_PATH}",
        "token_exchange_url": f"{VALID}{TOKEN_EXCHANGE_PATH}",
        "webhook_url": f"{VALID}{WEBHOOK_PATH}",
    }
    # Paths match the routes that actually exist in this repository.
    assert ACCOUNT_LINK_PATH == "/integrations/ring/link"
    assert DEFAULT_REDIRECT_PATH == "/app/integrations/ring/devices"
    assert TOKEN_EXCHANGE_PATH == "/v1/integrations/ring/token-exchange"  # noqa: S105 - a route
    assert WEBHOOK_PATH == "/v1/providers/ring/webhooks"
    for url in urls.values():
        assert url.startswith("https://")
        assert "@" not in url and "?" not in url and "#" not in url


def test_callback_paths_cannot_be_caller_controlled() -> None:
    origin = validate_public_origin(VALID)
    for hostile in ("../admin", "/a//b", "/trailing/", "no-leading-slash", "/x?y=1#z"):
        with pytest.raises(InvalidPublicOrigin):
            origin.url_for(hostile)


def test_host_header_cannot_change_callbacks() -> None:
    """Callbacks come from configuration, so a forged Host/X-Forwarded-Host changes nothing."""
    configured = validate_public_origin(VALID).callback_urls()
    attacker_supplied = "https://attacker.example.test"
    assert validate_public_origin(attacker_supplied).callback_urls() != configured
    # The derivation function takes no request object at all: there is no path for a header to
    # reach it, which is the property this test pins.
    assert callback_urls_for(VALID) == configured


def test_settings_reject_an_unsafe_origin_and_accept_a_valid_one() -> None:
    base = {
        "environment": "test",
        "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
        "app_version": "0.0.0-test",
        "_env_file": None,
    }
    assert Settings(**base).public_origin == ""  # unset by default; readiness reports missing
    assert Settings(**base, public_origin=VALID).public_origin == VALID
    for unsafe in ("http://ring.example.test", "https://localhost", "https://a.test/path"):
        with pytest.raises(ValueError):
            Settings(**base, public_origin=unsafe)
